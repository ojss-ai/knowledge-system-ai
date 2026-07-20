"""
Knowledge node service: CRUD, revisions, wikilink resolution, soft-delete.

Every read goes through app/services/visibility.py (ADR-004).
PG is the source of truth; Neo4j sync is best-effort post-write (ADR-011).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.models.knowledge import KnowledgeNode, NodeRevision, NodeType
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
    await db.flush()
    # NOTE: Neo4j vertex upsert happens AFTER db.commit() in the calling code path.
    # node_service.create_node() callers must call gs.upsert_vertex(node) post-commit,
    # or the router does so after awaiting the service (see kb-neo4j-graph skill).
    return node


async def get_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> KnowledgeNode:
    clause = visible_nodes_clause(viewer)
    row = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node_id).where(clause))
    if row is None:
        # Distinguish not-found from forbidden
        exists = await db.scalar(
            select(KnowledgeNode.id).where(
                KnowledgeNode.id == node_id, KnowledgeNode.deleted_at.is_(None)
            )
        )
        if exists is None:
            raise NotFoundError(f"Node {node_id} not found")
        raise ForbiddenError(f"Node {node_id} not accessible")
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

    # Save revision before mutating
    rev_count = (
        await db.scalar(
            select(func.count()).select_from(NodeRevision).where(NodeRevision.node_id == node_id)
        )
        or 0
    )
    revision = NodeRevision(
        id=uuid.uuid4(),
        node_id=node.id,
        version=rev_count + 1,
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
    await db.flush()
    await _graph_sync(gs.upsert_vertex(node))  # sync vertex props
    return node


async def delete_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> None:
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can delete a node")
    node.deleted_at = datetime.now(UTC)
    await db.flush()
    await _graph_sync(gs.soft_delete_vertex(node_id))


async def resolve_wikilinks(db: AsyncSession, node: KnowledgeNode, viewer: Viewer) -> None:
    """
    Find [[Title]] references in node.body, resolve to node IDs by title,
    and MERGE LINKS_TO edges in Neo4j via graph_service.
    Unresolved titles are silently skipped.
    """
    titles = _WIKILINK_RE.findall(node.body)
    if not titles:
        return

    # Ensure source vertex exists
    await _graph_sync(gs.upsert_vertex(node))

    clause = visible_nodes_clause(viewer)
    for title in set(titles):
        target = await db.scalar(
            select(KnowledgeNode).where(KnowledgeNode.title == title).where(clause).limit(1)
        )
        if target is None:
            continue
        await _graph_sync(gs.upsert_vertex(target))
        await _graph_sync(gs.merge_edge(node.id, target.id, "LINKS_TO", created_by="wikilink"))
