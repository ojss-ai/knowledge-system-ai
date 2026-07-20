import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.knowledge import KnowledgeNode, NodeShare, Tag
from app.models.user import Visibility

pytestmark = pytest.mark.asyncio


async def test_node_create_and_read(db, make_user, make_node):
    user = await make_user(email="node@test.com")
    node = await make_node(user, title="Hello", body="world", visibility=Visibility.private)
    await db.flush()

    result = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node.id))
    assert result is not None
    assert result.title == "Hello"
    assert result.body == "world"
    assert result.deleted_at is None
    assert result.body_tsv is not None  # GENERATED column populated


async def test_node_soft_delete(db, make_user, make_node):
    from datetime import UTC, datetime

    user = await make_user(email="del@test.com")
    node = await make_node(user)
    node.deleted_at = datetime.now(UTC)
    await db.flush()

    result = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node.id))
    assert result.deleted_at is not None


async def test_node_share(db, make_user, make_node):
    owner = await make_user(email="owner@test.com")
    other = await make_user(email="other@test.com")
    node = await make_node(owner, visibility=Visibility.shared)
    share = NodeShare(node_id=node.id, user_id=other.id)
    db.add(share)
    await db.flush()
    result = await db.scalar(
        select(NodeShare).where(NodeShare.node_id == node.id, NodeShare.user_id == other.id)
    )
    assert result is not None


async def test_tag_slug_unique(db):
    t1 = Tag(id=uuid.uuid4(), name="Python", slug="python")
    t2 = Tag(id=uuid.uuid4(), name="Python", slug="python")
    db.add(t1)
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():  # savepoint: keep the outer test txn usable
            db.add(t2)
            await db.flush()
