import uuid

import pytest
from sqlalchemy import select, text

from app.models.chunk import NodeChunk

pytestmark = pytest.mark.asyncio


async def test_chunk_create(db, make_user, make_node):
    owner = await make_user(email="chunk@test.com")
    node = await make_node(owner)
    await db.flush()
    chunk = NodeChunk(
        id=uuid.uuid4(),
        node_id=node.id,
        chunk_index=0,
        chunk_text="some text",
        embedding=[0.1] * 768,
    )
    db.add(chunk)
    await db.flush()
    result = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert result is not None
    assert len(result.embedding) == 768


async def test_hnsw_index_exists(db):
    result = await db.execute(
        text("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'node_chunks'
            AND indexdef ILIKE '%hnsw%'
        """)
    )
    rows = result.fetchall()
    assert len(rows) >= 1, "HNSW index on node_chunks.embedding must exist"
