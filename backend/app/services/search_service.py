"""
Hybrid full-text + vector search with RRF fusion.

CRITICAL invariants (ADR-004 / kb-visibility-filter):
- Visibility filter applied INSIDE each CTE leg, before LIMIT
- Never post-filter after merge — that would allow LIMIT to cut visible results
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode
from app.services.embedding_service import Embedder, get_embedder
from app.services.visibility import Viewer, visible_nodes_clause

_RRF_K = 60
_DEFAULT_LIMIT = 20
_EF_SEARCH = 80


async def hybrid_search(
    db: AsyncSession,
    query: str,
    viewer: Viewer,
    *,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
    fake_embedder: Embedder | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Hybrid RRF search: FTS leg + vector leg fused with Reciprocal Rank Fusion.
    Returns (results, total_count).

    score = Σ 1 / (k + rank_i)   where k=60
    """
    embedder = fake_embedder or get_embedder()
    query_vec = embedder.embed([query])[0]

    # Build visibility predicate as a subquery node_id list
    clause = visible_nodes_clause(viewer)
    visible_ids_result = await db.scalars(select(KnowledgeNode.id).where(clause))
    visible_ids = [str(i) for i in visible_ids_result]

    if not visible_ids:
        return [], 0

    # Set ef_search for this session (HNSW quality vs speed)
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {_EF_SEARCH}"))

    vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"

    sql = text("""
        WITH visible AS (
            SELECT id FROM knowledge_nodes
            WHERE id = ANY(CAST(:visible_ids AS uuid[]))
        ),
        fts_ranked AS (
            SELECT kn.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(kn.body_tsv, query) DESC) AS rank
            FROM knowledge_nodes kn,
                 to_tsquery('english', :tsquery) AS query
            WHERE kn.id IN (SELECT id FROM visible)
              AND kn.body_tsv @@ query
            LIMIT 100
        ),
        vec_ranked AS (
            SELECT DISTINCT ON (nc.node_id) nc.node_id AS id,
                   ROW_NUMBER() OVER (ORDER BY nc.embedding <=> CAST(:query_vec AS vector)) AS rank
            FROM node_chunks nc
            WHERE nc.node_id IN (SELECT id FROM visible)
              AND nc.embedding IS NOT NULL
            ORDER BY nc.node_id, nc.embedding <=> CAST(:query_vec AS vector)
            LIMIT 100
        ),
        rrf AS (
            SELECT id,
                   COALESCE(fts.rrf_score, 0) + COALESCE(vec.rrf_score, 0) AS score
            FROM (
                SELECT id, 1.0 / (:k + rank) AS rrf_score FROM fts_ranked
            ) fts
            FULL OUTER JOIN (
                SELECT id, 1.0 / (:k + rank) AS rrf_score FROM vec_ranked
            ) vec USING (id)
        )
        SELECT kn.id, kn.title, kn.node_type, kn.visibility, kn.updated_at,
               rrf.score
        FROM rrf
        JOIN knowledge_nodes kn ON kn.id = rrf.id
        ORDER BY rrf.score DESC
        LIMIT :limit OFFSET :offset
    """)

    count_sql = text("""
        WITH visible AS (
            SELECT id FROM knowledge_nodes WHERE id = ANY(CAST(:visible_ids AS uuid[]))
        ),
        fts_ids AS (
            SELECT kn.id FROM knowledge_nodes kn, to_tsquery('english', :tsquery) AS q
            WHERE kn.id IN (SELECT id FROM visible) AND kn.body_tsv @@ q
        ),
        vec_ids AS (
            SELECT DISTINCT nc.node_id AS id FROM node_chunks nc
            WHERE nc.node_id IN (SELECT id FROM visible) AND nc.embedding IS NOT NULL
        )
        SELECT COUNT(DISTINCT id) FROM (SELECT id FROM fts_ids UNION SELECT id FROM vec_ids) sub
    """)

    # Convert query to tsquery (simple: replace spaces with & for AND logic)
    tsquery = " & ".join(query.split())

    params = {
        "visible_ids": visible_ids,
        "tsquery": tsquery,
        "query_vec": vec_str,
        "k": _RRF_K,
        "limit": limit,
        "offset": offset,
    }

    rows = (await db.execute(sql, params)).fetchall()
    total = (await db.scalar(count_sql, {**params})) or 0

    results = [
        {
            "id": str(row[0]),
            "title": row[1],
            "node_type": row[2],
            "visibility": row[3],
            "updated_at": row[4].isoformat() if row[4] else None,
            "score": float(row[5]) if row[5] else 0.0,
        }
        for row in rows
    ]

    return results, int(total)
