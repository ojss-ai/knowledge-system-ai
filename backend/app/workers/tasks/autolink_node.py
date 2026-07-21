from __future__ import annotations

import asyncio
import uuid

from celery import Task
from pgvector.sqlalchemy import Vector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.models.user import Role
from app.services import graph_service as gs
from app.services.visibility import Viewer, visible_nodes_clause
from app.workers.celery_app import celery_app, task_session

_COSINE_THRESHOLD = 0.82
_TOP_K = 5


async def _autolink_node_impl(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> None:
    """
    Core logic extracted for unit-testability (no Celery dependency).

    Mean-pool the node's chunk vectors, find the top-K visible nodes whose best
    chunk has cosine >= threshold, and MERGE one SIMILAR_TO edge per pair with
    the lower node id as source (kb-pgvector-search). A re-run REPLACES the
    node's previous auto edges: stale created_by='system:autolink' edges are
    deleted before the new set is merged, so content drift never leaves ghost
    links. MERGE keeps the surviving set duplicate-free; there is no PG-side
    bookkeeping.

    `viewer` is the node OWNER's identity: a private node may auto-link only to
    nodes its owner can see (never SYSTEM_VIEWER — results become user-visible
    edges, so candidate reads must carry the owner's visibility).
    """
    source_node = await db.scalar(
        select(KnowledgeNode).where(KnowledgeNode.id == node_id, visible_nodes_clause(viewer))
    )
    if source_node is None:  # deleted or no longer visible: tolerate races
        return

    mean_vec = await db.scalar(
        select(func.avg(NodeChunk.embedding, type_=Vector(768))).where(
            NodeChunk.node_id == node_id, NodeChunk.embedding.is_not(None)
        )
    )
    if mean_vec is None:  # node has no embedded chunks yet
        return

    # Re-run replaces the node's previous auto edges (kb-pgvector-search): drop
    # stale system:autolink edges BEFORE merging, and before the early return
    # below — content drift may leave an empty candidate set, and the old edges
    # must still disappear. Manual SIMILAR_TO edges survive (created_by filter).
    await gs.delete_autolink_edges(node_id)

    # Best chunk per candidate node: MIN cosine distance == MAX cosine similarity.
    # Visibility applied INSIDE the query, before HAVING/LIMIT (kb-visibility-filter).
    distance = NodeChunk.embedding.cosine_distance(mean_vec)
    best_dist = func.min(distance)
    rows = (
        await db.execute(
            select(NodeChunk.node_id, (1 - best_dist).label("score"))
            .join(KnowledgeNode, KnowledgeNode.id == NodeChunk.node_id)
            .where(
                visible_nodes_clause(viewer),
                NodeChunk.node_id != node_id,
                NodeChunk.embedding.is_not(None),
            )
            .group_by(NodeChunk.node_id)
            .having(best_dist <= 1.0 - _COSINE_THRESHOLD)
            # node_id tiebreak keeps the top-K deterministic across re-runs
            .order_by(best_dist, NodeChunk.node_id)
            .limit(_TOP_K)
        )
    ).all()
    if not rows:
        return

    scores: dict[uuid.UUID, float] = {row.node_id: float(row.score) for row in rows}
    targets = (
        await db.scalars(
            select(KnowledgeNode).where(KnowledgeNode.id.in_(scores), visible_nodes_clause(viewer))
        )
    ).all()

    # Vertices must exist before MERGE edge Cypher can MATCH them.
    await gs.upsert_vertex(source_node)
    for target in targets:
        await gs.upsert_vertex(target)
        src_id, tgt_id = sorted((node_id, target.id))  # one edge per pair, lower id as source
        await gs.merge_edge(
            src_id,
            tgt_id,
            "SIMILAR_TO",
            created_by="system:autolink",
            score=scores[target.id],
        )


@celery_app.task(  # type: ignore[untyped-decorator]  # celery is untyped (ignore_missing_imports)
    bind=True,
    name="kb.autolink_node",
    queue="default",  # light DB/graph I/O, not CPU-bound (kb-celery-jobs rule 6)
    acks_late=True,
    max_retries=3,
    retry_backoff=True,
)
def autolink_node(self: Task, node_id: str, user_id: str, role: str, group_ids: list[str]) -> None:
    """
    Celery task: create SIMILAR_TO edges for a node after (re)embedding.
    Args must be primitives; (user_id, role, group_ids) is the node owner's viewer.
    """
    viewer = Viewer(
        user_id=uuid.UUID(user_id),
        role=Role(role),
        group_ids=frozenset(uuid.UUID(gid) for gid in group_ids),
    )
    nid = uuid.UUID(node_id)

    async def _run() -> None:
        async with task_session() as db:
            await _autolink_node_impl(db, nid, viewer)

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise self.retry(exc=exc) from exc
