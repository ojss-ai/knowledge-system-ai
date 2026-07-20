import pytest
from sqlalchemy import func, select

from app.models.chunk import NodeChunk
from app.workers.tasks.embed_node import _embed_node_impl, embed_node

pytestmark = pytest.mark.asyncio


async def test_embed_node_task_options():
    """Canonical kb-celery-jobs task shape: embed queue, backoff retries, late acks."""
    assert embed_node.queue == "embed", "CPU-bound embedding must route to the embed queue"
    assert embed_node.retry_backoff is True
    assert embed_node.acks_late is True
    assert embed_node.max_retries == 3


async def test_embed_node_creates_chunks(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed1@test.com")
    node = await make_node(owner, body="# Section\n\nThis is content for embedding.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count >= 1


async def test_embed_node_idempotent(db, make_user, make_node, fake_embedder):
    """Running embed twice must NOT create duplicate chunks."""
    owner = await make_user(email="embed2@test.com")
    node = await make_node(owner, body="# A\n\nContent.\n\n# B\n\nMore content.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    count_after_first = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )

    await _embed_node_impl(db, node.id, fake_embedder)
    count_after_second = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )

    assert count_after_first == count_after_second, (
        "Re-running embed must not create duplicate chunks"
    )


async def test_reembed_empty_body_clears_stale_chunks(db, make_user, make_node, fake_embedder):
    """Editing a body down to nothing must delete the old chunks on re-embed."""
    owner = await make_user(email="embed5@test.com")
    node = await make_node(owner, body="# Was here\n\nContent that will be erased.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count >= 1

    node.body = ""
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count == 0, "Stale chunks must be deleted when the new chunk list is empty"


async def test_embed_node_skips_soft_deleted(db, make_user, make_node, fake_embedder):
    """A soft-deleted node must not be (re)embedded — no chunks written (SYSTEM_VIEWER path)."""
    from datetime import UTC, datetime

    owner = await make_user(email="embed4@test.com")
    node = await make_node(owner, body="# Gone\n\nDeleted content.")
    node.deleted_at = datetime.now(UTC)
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count == 0


async def test_embed_node_stores_vectors(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed3@test.com")
    node = await make_node(owner, body="Some text to embed.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    chunk = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert chunk.embedding is not None
    assert len(chunk.embedding) == 768


async def test_embed_failure_propagates_for_retry(db, make_user, make_node):
    """Transient embedder failures must escape _embed_node_impl (not be swallowed),
    so the Celery wrapper's `raise self.retry(exc=exc)` fires. The bound task's
    retry itself needs a broker and is exercised only in integration
    (kb-celery-jobs: no live broker in unit tests)."""

    class BoomEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("transient embedding backend failure")

    owner = await make_user(email="embed6@test.com")
    node = await make_node(owner, body="Content that will fail to embed.")
    await db.flush()

    with pytest.raises(RuntimeError, match="transient embedding backend failure"):
        await _embed_node_impl(db, node.id, BoomEmbedder())
