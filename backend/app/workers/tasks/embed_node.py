from __future__ import annotations

import asyncio
import uuid

from celery import Task
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.services.chunking import chunk_markdown
from app.services.embedding_service import Embedder, get_embedder
from app.services.visibility import SYSTEM_VIEWER, visible_nodes_clause
from app.workers.celery_app import celery_app, task_session


async def _embed_node_impl(db: AsyncSession, node_id: uuid.UUID, embedder: Embedder) -> None:
    """
    Core logic extracted for unit-testability (no Celery dependency).
    Idempotent: deletes existing chunks for the node before reinserting.
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
        return

    texts = chunk_markdown(node.body)

    # Idempotent replace: the delete ALWAYS runs, even when the new chunk list
    # is empty — a body edited down to nothing must clear its stale chunks.
    await db.execute(delete(NodeChunk).where(NodeChunk.node_id == node_id))

    if not texts:
        return

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
    Celery task: chunk and embed a knowledge node.
    Args must be primitives (str, not UUID).
    """
    nid = uuid.UUID(node_id)
    embedder = get_embedder()

    async def _run() -> None:
        async with task_session() as db:
            await _embed_node_impl(db, nid, embedder)

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise self.retry(exc=exc) from exc
