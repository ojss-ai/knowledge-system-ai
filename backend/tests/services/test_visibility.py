import pytest
from sqlalchemy import select

from app.models.knowledge import KnowledgeNode
from app.models.user import Role, Visibility
from app.services.visibility import Viewer, shared_node_ids, visible_nodes_clause

pytestmark = pytest.mark.asyncio


async def test_private_node_invisible_to_others(db, make_user, make_node):
    owner = await make_user(email="v_owner@test.com")
    other = await make_user(email="v_other@test.com")
    node = await make_node(owner, visibility=Visibility.private)
    await db.flush()

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id not in ids, "Private node must NOT be visible to non-owner"


async def test_private_node_visible_to_owner(db, make_user, make_node):
    owner = await make_user(email="v_owner2@test.com")
    node = await make_node(owner, visibility=Visibility.private)
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Owner must see their own private node"


async def test_public_node_visible_to_all(db, make_user, make_node):
    owner = await make_user(email="v_pub@test.com")
    other = await make_user(email="v_pub2@test.com")
    node = await make_node(owner, visibility=Visibility.public)
    await db.flush()

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Public node must be visible to all"


async def test_shared_node_visible_to_share_target(db, make_user, make_node):
    from app.models.knowledge import NodeShare

    owner = await make_user(email="v_sh_owner@test.com")
    target = await make_user(email="v_sh_target@test.com")
    node = await make_node(owner, visibility=Visibility.shared)
    share = NodeShare(node_id=node.id, user_id=target.id)
    db.add(share)
    await db.flush()

    viewer = Viewer(user_id=target.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Shared node must be visible to share target"


async def test_shared_node_ids_returns_direct_shares(db, make_user, make_node):
    from app.models.knowledge import NodeShare

    owner = await make_user(email="v_sni_owner@test.com")
    target = await make_user(email="v_sni_target@test.com")
    node = await make_node(owner, visibility=Visibility.shared)
    db.add(NodeShare(node_id=node.id, user_id=target.id))
    await db.flush()

    viewer = Viewer(user_id=target.id, role=Role.user, group_ids=frozenset())
    ids = await shared_node_ids(viewer, db)
    assert node.id in ids, "Directly shared node id must be returned"


async def test_admin_sees_all(db, make_user, make_node):
    owner = await make_user(email="v_adm_owner@test.com")
    admin = await make_user(email="v_admin@test.com", role=Role.admin)
    node = await make_node(owner, visibility=Visibility.private)
    await db.flush()

    viewer = Viewer(user_id=admin.id, role=Role.admin, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Admin must see all nodes"


async def test_deleted_nodes_excluded(db, make_user, make_node):
    from datetime import UTC, datetime

    owner = await make_user(email="v_del@test.com")
    node = await make_node(owner, visibility=Visibility.public)
    node.deleted_at = datetime.now(UTC)
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id not in ids, "Soft-deleted node must be excluded"
