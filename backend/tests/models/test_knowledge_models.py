import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.group import Group
from app.models.knowledge import KnowledgeNode, NodeRevision, NodeShare, Tag
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


async def test_share_requires_exactly_one_grantee_neither(db, make_user, make_node):
    """A NodeShare with neither user_id nor group_id must be rejected (XOR check)."""
    owner = await make_user(email="xor-none@test.com")
    node = await make_node(owner, visibility=Visibility.shared)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(NodeShare(node_id=node.id, user_id=None, group_id=None))
            await db.flush()


async def test_share_requires_exactly_one_grantee_both(db, make_user, make_node):
    """A NodeShare with both user_id and group_id set must be rejected (XOR check)."""
    owner = await make_user(email="xor-both@test.com")
    other = await make_user(email="xor-both-other@test.com")
    group = Group(id=uuid.uuid4(), name="xor-group", created_by=owner.id)
    db.add(group)
    await db.flush()
    node = await make_node(owner, visibility=Visibility.shared)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(NodeShare(node_id=node.id, user_id=other.id, group_id=group.id))
            await db.flush()


async def test_revision_version_unique_per_node(db, make_user, make_node):
    """Revisions accumulate per node; version is unique per node but reusable across nodes."""
    user = await make_user(email="rev@test.com")
    node = await make_node(user, title="v1", body="first")
    other_node = await make_node(user, title="other")

    def _rev(target, version: int) -> NodeRevision:
        return NodeRevision(
            node_id=target.id,
            version=version,
            title_snapshot=target.title,
            body_snapshot=target.body,
            changed_by=user.id,
        )

    db.add_all([_rev(node, 1), _rev(node, 2)])
    await db.flush()

    revisions = (
        await db.scalars(
            select(NodeRevision)
            .where(NodeRevision.node_id == node.id)
            .order_by(NodeRevision.version)
        )
    ).all()
    assert [r.version for r in revisions] == [1, 2]

    # Same version on a different node is fine.
    db.add(_rev(other_node, 1))
    await db.flush()

    # Duplicate version on the same node violates uq_revision_version.
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(_rev(node, 2))
            await db.flush()


async def test_tag_slug_unique(db):
    t1 = Tag(id=uuid.uuid4(), name="Python", slug="python")
    t2 = Tag(id=uuid.uuid4(), name="Python", slug="python")
    db.add(t1)
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():  # savepoint: keep the outer test txn usable
            db.add(t2)
            await db.flush()
