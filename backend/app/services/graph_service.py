"""
Neo4j graph operations for the knowledge graph (ADR-011).

Rules (from kb-neo4j-graph skill):
- All driver calls go through this module only — no neo4j imports elsewhere.
- MERGE, not CREATE, for edges (idempotent).
- upsert_vertex / merge_edge / delete_edge are called AFTER the PG commit.
- Hop limit ≤ 3, node limit ≤ 500.
- Visibility filter applied via PG (the authoritative source) AFTER traversal.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.neo4j import get_driver
from app.models.knowledge import KnowledgeNode
from app.services.visibility import Viewer, visible_nodes_clause

_HOP_LIMIT = 3
_NODE_LIMIT = 500

ALLOWED_EDGE_LABELS = frozenset({
    "LINKS_TO", "REFERENCES", "DERIVED_FROM", "TAGGED_WITH", "SIMILAR_TO",
    "PARENT_OF", "AUTHORED_BY", "MENTIONS", "IMPORTS", "CALLS", "DEFINES",
    "BELONGS_TO_PROJECT",
})


async def upsert_vertex(node: KnowledgeNode) -> None:
    """Create or update a :Node vertex in Neo4j. Call AFTER PG commit."""
    async with get_driver().session() as session:
        await session.run(
            """
            MERGE (n:Node {node_id: $node_id})
            SET n.title      = $title,
                n.node_type  = $node_type,
                n.visibility = $visibility,
                n.owner_id   = $owner_id,
                n.deleted    = false
            """,
            node_id=str(node.id),
            title=node.title,
            node_type=node.node_type,
            visibility=node.visibility.value,
            owner_id=str(node.owner_id),
        )


async def soft_delete_vertex(node_id: uuid.UUID) -> None:
    """Mark vertex deleted. Call AFTER PG soft-delete commit."""
    async with get_driver().session() as session:
        await session.run(
            "MATCH (n:Node {node_id: $node_id}) SET n.deleted = true",
            node_id=str(node_id),
        )


async def merge_edge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    label: str,
    created_by: str,
    score: float | None = None,
) -> None:
    """MERGE a directed edge. label must be in ALLOWED_EDGE_LABELS."""
    assert label in ALLOWED_EDGE_LABELS, f"Unknown edge label: {label}"
    async with get_driver().session() as session:
        await session.run(
            f"""
            MATCH (a:Node {{node_id: $src}}), (b:Node {{node_id: $tgt}})
            MERGE (a)-[r:{label}]->(b)
            SET r.created_by = $created_by,
                r.score      = $score
            """,
            src=str(source_id),
            tgt=str(target_id),
            created_by=created_by,
            score=score,
        )


async def delete_edge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    label: str,
) -> None:
    assert label in ALLOWED_EDGE_LABELS, f"Unknown edge label: {label}"
    async with get_driver().session() as session:
        await session.run(
            f"""
            MATCH (a:Node {{node_id: $src}})-[r:{label}]->(b:Node {{node_id: $tgt}})
            DELETE r
            """,
            src=str(source_id),
            tgt=str(target_id),
        )


async def get_neighborhood(
    db: AsyncSession,
    center_id: uuid.UUID,
    viewer: Viewer,
    hops: int = 1,
) -> dict[str, list[dict[str, Any]]]:
    """
    Return nodes and edges within `hops` hops of center_id, visibility-filtered.
    hops clamped to _HOP_LIMIT.  Total nodes capped at _NODE_LIMIT.
    Visibility is enforced by re-querying PG (the authoritative source).
    """
    hops = max(0, min(hops, _HOP_LIMIT))  # defense-in-depth: interpolated into the pattern
    candidate_ids: set[uuid.UUID] = {center_id}
    raw_edges: list[dict[str, Any]] = []

    async with get_driver().session() as session:
        # LIMIT must bound the per-neighbor rows BEFORE aggregation: after
        # collect() the match collapses to a single row and LIMIT is a no-op,
        # letting a hub node pull an unbounded set into Python.
        result = await session.run(
            f"""
            MATCH (center:Node {{node_id: $cid}})-[e*0..{hops}]-(other:Node)
            WHERE other.deleted IS NULL OR other.deleted = false
            WITH DISTINCT other, e
            LIMIT $limit
            WITH collect(DISTINCT other) AS nodes,
                 collect(DISTINCT e)    AS edge_lists
            RETURN nodes, edge_lists
            """,
            cid=str(center_id),
            limit=_NODE_LIMIT,
        )
        record = await result.single()

    if record:
        for n in record["nodes"]:
            nid = n.get("node_id")
            if nid:
                try:
                    candidate_ids.add(uuid.UUID(nid))
                except ValueError:
                    pass
        for path_edges in record["edge_lists"]:
            if isinstance(path_edges, list):
                for e in path_edges:
                    raw_edges.append({
                        "source": e.start_node["node_id"] if hasattr(e, "start_node") else None,
                        "target": e.end_node["node_id"] if hasattr(e, "end_node") else None,
                        "label": e.type if hasattr(e, "type") else "",
                    })

    # Apply visibility filter via Postgres (authoritative)
    if not candidate_ids:
        return {"nodes": [], "edges": []}

    clause = visible_nodes_clause(viewer)
    visible_rows = await db.scalars(
        select(KnowledgeNode)
        .where(KnowledgeNode.id.in_(list(candidate_ids)))
        .where(clause)
    )
    visible_nodes = list(visible_rows)
    visible_ids = {str(n.id) for n in visible_nodes}

    nodes_out: list[dict[str, Any]] = [
        {
            "id": str(n.id),
            "title": n.title,
            "node_type": n.node_type,
            "visibility": n.visibility.value,
        }
        for n in visible_nodes
    ]
    edges_out = [
        e for e in raw_edges
        if e["source"] in visible_ids and e["target"] in visible_ids
    ]
    return {"nodes": nodes_out, "edges": edges_out}


async def get_overview(
    db: AsyncSession,
    viewer: Viewer,
    limit: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    """Top visible nodes + edges between them for the initial graph viewport."""
    clause = visible_nodes_clause(viewer)
    rows = await db.scalars(
        select(KnowledgeNode)
        .where(clause)
        .order_by(KnowledgeNode.updated_at.desc())
        .limit(limit)
    )
    nodes = list(rows)
    id_set = {str(n.id) for n in nodes}

    nodes_out: list[dict[str, Any]] = [
        {"id": str(n.id), "title": n.title, "node_type": n.node_type} for n in nodes
    ]

    if not id_set:
        return {"nodes": nodes_out, "edges": []}

    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (a:Node)-[r]->(b:Node)
            WHERE a.node_id IN $ids AND b.node_id IN $ids
            RETURN a.node_id AS src, b.node_id AS tgt, type(r) AS lbl
            LIMIT $limit
            """,
            ids=list(id_set),
            limit=_NODE_LIMIT,
        )
        records = await result.data()

    edges_out = [{"source": r["src"], "target": r["tgt"], "label": r["lbl"]} for r in records]
    return {"nodes": nodes_out, "edges": edges_out}
