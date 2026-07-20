import pytest
from sqlalchemy import func, select

from app.models.chunk import NodeChunk
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


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


async def test_embed_node_stores_vectors(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed3@test.com")
    node = await make_node(owner, body="Some text to embed.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    chunk = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert chunk.embedding is not None
    assert len(chunk.embedding) == 768
