from __future__ import annotations

import asyncio
import uuid

from celery import Task
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.group import GroupMember
from app.models.knowledge import KnowledgeNode
from app.models.user import User
from app.services.chunking import chunk_markdown
from app.services.embedding_service import Embedder, get_embedder
from app.services.visibility import SYSTEM_VIEWER, visible_nodes_clause
from app.workers.celery_app import celery_app, task_session
from app.workers.tasks.autolink_node import autolink_node

# Primitive args for chaining autolink_node.delay: (node_id, user_id, role, group_ids)
AutolinkArgs = tuple[str, str, str, list[str]]


async def _embed_node_impl(
    db: AsyncSession, node_id: uuid.UUID, embedder: Embedder
) -> KnowledgeNode | None:
    """
    Core logic extracted for unit-testability (no Celery dependency).
    Idempotent: deletes existing chunks for the node before reinserting.
    Returns the embedded node, or None when it is gone/soft-deleted.
    """
    # SYSTEM_VIEWER justification (kb-visibility-filter rule 1): embedding is a
    # system job that must (re)index any LIVE node regardless of owner. Going
    # through visible_nodes_clause keeps the single visibility choke point and
    # still excludes soft-deleted rows; the result is never shown to a user.
    node = await db.scalar(
        select(KnowledgeNode).where(
            KnowledgeNode.id == node_id,
            visible_nodes_clause(SYSTEM_VIEWER),
        )
    )
    if node is None:
        return None

    texts = chunk_markdown(node.body)

    # Idempotent replace: the delete ALWAYS runs, even when the new chunk list
    # is empty — a body edited down to nothing must clear its stale chunks.
    await db.execute(delete(NodeChunk).where(NodeChunk.node_id == node_id))

    if not texts:
        return node

    vectors = embedder.embed(texts)

    for idx, (text, vec) in enumerate(zip(texts, vectors, strict=True)):
        chunk = NodeChunk(
            node_id=node_id,
            chunk_index=idx,
            chunk_text=text,
            embedding=vec,
        )
        db.add(chunk)

    await db.flush()
    return node


async def _embed_and_prepare_autolink(
    db: AsyncSession, node_id: uuid.UUID, embedder: Embedder
) -> AutolinkArgs | None:
    """
    Embed the node, then build the primitive args for chaining autolink_node
    (plan Goal: auto-linking runs as a post-embed task). Runs INSIDE the task
    session; the actual .delay happens post-commit via _after_embed.

    The args are the node OWNER's viewer (user id, role, group ids): autolink's
    candidate reads must carry the owner's visibility, never SYSTEM_VIEWER.
    Returns None (no chaining) when the node or its owner is gone.
    """
    node = await _embed_node_impl(db, node_id, embedder)
    if node is None:
        return None

    owner_role = await db.scalar(select(User.role).where(User.id == node.owner_id))
    if owner_role is None:  # owner row gone (FK race): nothing to link on behalf of
        return None
    group_ids = await db.scalars(
        select(GroupMember.group_id).where(GroupMember.user_id == node.owner_id)
    )
    return (
        str(node.id),
        str(node.owner_id),
        owner_role.value,
        sorted(str(gid) for gid in group_ids),
    )


def _after_embed(autolink_args: AutolinkArgs | None) -> None:
    """
    Post-commit hook: chain autolink via the queue, never inline
    (kb-celery-jobs rule 7). Must be called AFTER task_session commits so the
    autolink worker sees the freshly written chunks.
    """
    if autolink_args is None:
        return
    autolink_node.delay(*autolink_args)


@celery_app.task(  # type: ignore[untyped-decorator]  # celery is untyped (ignore_missing_imports)
    bind=True,
    name="kb.embed_node",
    queue="embed",  # CPU/GPU-bound work (kb-celery-jobs rule 6); workers must consume -Q embed
    acks_late=True,
    max_retries=3,
    retry_backoff=True,
)
def embed_node(self: Task, node_id: str) -> None:
    """
    Celery task: chunk and embed a knowledge node, then chain autolink_node
    (post-embed task, kb-celery-jobs rule 7). Args must be primitives (str,
    not UUID).
    """
    nid = uuid.UUID(node_id)
    embedder = get_embedder()

    async def _run() -> AutolinkArgs | None:
        async with task_session() as db:
            return await _embed_and_prepare_autolink(db, nid, embedder)

    try:
        autolink_args = asyncio.run(_run())
    except Exception as exc:
        raise self.retry(exc=exc) from exc

    # Chain AFTER the session above committed: the autolink worker must see the
    # new chunks. Enqueue failures propagate and fail the task; embed_node is
    # idempotent, so a re-run (manual or requeued) is always safe.
    _after_embed(autolink_args)
