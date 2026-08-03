"""
Knowledge node service: CRUD, revisions, wikilink resolution, soft-delete.

Every read goes through app/services/visibility.py (ADR-004).

PG is the source of truth; Neo4j is synced strictly AFTER the PG commit
(ADR-011, CLAUDE.md "PG first, Neo4j second"). Mutation functions never touch
Neo4j themselves — they queue pending graph operations on the session
(``db.info["pending_graph_ops"]``) and the caller (router/worker) runs them
once the transaction is durable:

    await db.commit()
    await node_service.run_pending_graph_ops(db)

Each op is best-effort: a Neo4j failure is logged and never fails (or rolls
back) the relational write. If the session rolls back, the queued ops are
simply discarded with it (they are only executed explicitly).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from functools import partial
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.knowledge import KnowledgeNode, NodeRevision, NodeShare, NodeType
from app.models.user import Role, Visibility
from app.services import graph_service as gs
from app.services.visibility import Viewer, visible_nodes_clause

logger = structlog.get_logger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


async def _graph_sync(op: Coroutine[Any, Any, None]) -> None:
    """Best-effort Neo4j sync — PG is the source of truth (ADR-011).

    A graph failure must never fail (or roll back) the relational write.
    TODO(workers phase): enqueue tasks.sync_graph_vertex retry instead of log-only.
    """
    try:
        await op
    except Exception as exc:
        logger.warning("graph_sync_failed", error=str(exc))


# A pending graph operation: zero-arg callable producing the coroutine to await.
# Stored as partials (not live coroutines) so discarding them on rollback is safe.
GraphOp = Callable[[], Coroutine[Any, Any, None]]

_PENDING_KEY = "pending_graph_ops"


def queue_graph_op(db: AsyncSession, op: GraphOp) -> None:
    """Queue a Neo4j op to run after the PG commit — never inside it (ADR-011).

    Public: ingest services (app/services/ingest/) queue their edge MERGEs
    through the same session mechanism, so run_pending_graph_ops() drains
    node and ingest ops together, in order.
    """
    db.info.setdefault(_PENDING_KEY, []).append(op)


def pending_graph_ops(db: AsyncSession) -> list[GraphOp]:
    """Graph ops queued on this session, awaiting run_pending_graph_ops()."""
    ops: list[GraphOp] = db.info.get(_PENDING_KEY, [])
    return list(ops)


async def run_pending_graph_ops(db: AsyncSession) -> None:
    """Run (and clear) the session's queued Neo4j ops.

    Callers MUST invoke this AFTER ``db.commit()``. Best-effort: each op is
    wrapped in _graph_sync, so a Neo4j failure is logged, never raised.
    """
    ops: list[GraphOp] = db.info.pop(_PENDING_KEY, [])
    for op in ops:
        await _graph_sync(op())


async def create_node(
    db: AsyncSession,
    *,
    viewer: Viewer,
    title: str,
    body: str = "",
    node_type: str = NodeType.note.value,
    visibility: Visibility = Visibility.private,
    source: str | None = None,
    source_ref: str | None = None,
    meta: dict[str, Any] | None = None,
) -> KnowledgeNode:
    node = KnowledgeNode(
        id=uuid.uuid4(),
        owner_id=viewer.user_id,
        title=title,
        body=body,
        node_type=node_type,
        visibility=visibility,
        source=source,
        source_ref=source_ref,
        meta=meta or {},
    )
    db.add(node)
    try:
        await db.flush()
    except IntegrityError as exc:
        # A row with this (owner_id, source, source_ref) already exists — e.g. a
        # concurrent double-POST that slipped past a SELECT-then-INSERT probe.
        # Surface as 409 (mirrors uq_revision_version in update_node); upsert-style
        # callers catch it, re-fetch, and update the existing row instead.
        if "uq_node_owner_source_ref" in str(exc.orig):
            raise ConflictError(
                f"Node with source={source!r} source_ref={source_ref!r} "
                "already exists for this owner"
            ) from exc
        raise
    # Neo4j vertex upsert runs AFTER db.commit(): queued here, executed by the
    # caller via run_pending_graph_ops(db) (module docstring, kb-neo4j-graph).
    queue_graph_op(db, partial(gs.upsert_vertex, node))
    return node


async def get_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> KnowledgeNode:
    """Invisible nodes are indistinguishable from nonexistent ones (ADR-004,
    kb-visibility-filter): both raise NotFoundError. A 403 here would confirm
    to any authenticated caller that the private node id exists."""
    clause = visible_nodes_clause(viewer)
    row = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node_id).where(clause))
    if row is None:
        # Do not echo the id back: the message must not confirm anything
        # about what exists (the caller already knows what id they asked for,
        # but keeping the body generic makes existence-leak tests meaningful).
        raise NotFoundError("Node not found")
    return row


async def list_nodes(
    db: AsyncSession,
    viewer: Viewer,
    *,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[KnowledgeNode], int]:
    clause = visible_nodes_clause(viewer)
    total = await db.scalar(select(func.count()).select_from(KnowledgeNode).where(clause)) or 0
    rows = await db.scalars(
        select(KnowledgeNode)
        .where(clause)
        .order_by(KnowledgeNode.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows), total


async def update_node(
    db: AsyncSession,
    node_id: uuid.UUID,
    viewer: Viewer,
    *,
    title: str | None = None,
    body: str | None = None,
    visibility: Visibility | None = None,
    meta: dict[str, Any] | None = None,
) -> KnowledgeNode:
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can edit a node")

    # Save revision before mutating. Lock the node row so concurrent updates
    # serialize their revision numbering, then take max(version)+1 —
    # COUNT(*)+1 is racy and breaks when versions have gaps.
    await db.execute(select(KnowledgeNode.id).where(KnowledgeNode.id == node_id).with_for_update())
    max_version = (
        await db.scalar(
            select(func.max(NodeRevision.version)).where(NodeRevision.node_id == node_id)
        )
        or 0
    )
    revision = NodeRevision(
        id=uuid.uuid4(),
        node_id=node.id,
        version=max_version + 1,
        title_snapshot=node.title,
        body_snapshot=node.body,
        changed_by=viewer.user_id,
    )
    db.add(revision)

    if title is not None:
        node.title = title
    if body is not None:
        node.body = body
    if visibility is not None:
        node.visibility = visibility
    if meta is not None:
        node.meta = {**node.meta, **meta}

    node.updated_at = datetime.now(UTC)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Residual duplicate (node_id, version) despite the row lock —
        # e.g. a writer path that skipped the lock. Surface as a 409.
        if "uq_revision_version" in str(exc.orig):
            raise ConflictError(f"Concurrent revision conflict for node {node_id}") from exc
        raise
    queue_graph_op(db, partial(gs.upsert_vertex, node))  # vertex props, post-commit
    return node


async def delete_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> None:
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can delete a node")
    node.deleted_at = datetime.now(UTC)
    await db.flush()
    queue_graph_op(db, partial(gs.soft_delete_vertex, node_id))


async def share_node(
    db: AsyncSession,
    node_id: uuid.UUID,
    viewer: Viewer,
    *,
    user_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    can_edit: bool = False,
) -> KnowledgeNode:
    """Add a node_shares row. Only the owner (or admin) may extend visibility —
    letting any viewer share would leak nodes shared *with* them (ADR-004)."""
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can share a node")
    share = NodeShare(
        id=uuid.uuid4(), node_id=node.id, user_id=user_id, group_id=group_id, can_edit=can_edit
    )
    db.add(share)
    await db.flush()
    return node


async def resolve_wikilinks(db: AsyncSession, node: KnowledgeNode, viewer: Viewer) -> None:
    """
    Find [[Title]] references in node.body, resolve to node IDs by title,
    and queue LINKS_TO edge MERGEs for post-commit run_pending_graph_ops().
    Unresolved titles and self-references ([[Own Title]]) are silently skipped.
    """
    titles = _WIKILINK_RE.findall(node.body)
    if not titles:
        return

    # Ensure source vertex exists (queued: runs post-commit, before the edges)
    queue_graph_op(db, partial(gs.upsert_vertex, node))

    clause = visible_nodes_clause(viewer)
    for title in set(titles):
        if title == node.title:
            continue  # self-link guard: [[Own Title]] creates no edge
        # Titles are not unique; MVP behavior is "first visible match wins"
        # (.limit(1)). Revisit if titles ever get a uniqueness rule.
        target = await db.scalar(
            select(KnowledgeNode).where(KnowledgeNode.title == title).where(clause).limit(1)
        )
        if target is None or target.id == node.id:
            continue
        queue_graph_op(db, partial(gs.upsert_vertex, target))
        queue_graph_op(
            db, partial(gs.merge_edge, node.id, target.id, "LINKS_TO", created_by="wikilink")
        )
