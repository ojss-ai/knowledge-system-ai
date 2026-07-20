import pytest

from app.models.user import Role, Visibility
from app.services import graph_service as gs
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_create_vertex(db, neo4j_session, make_user, make_node):
    owner = await make_user(email="gs_v@test.com")
    node = await make_node(owner, title="Graph Node")
    await db.commit()
    await gs.upsert_vertex(node)
    # vertex should exist in Neo4j
    result = await neo4j_session.run(
        "MATCH (n:Node {node_id: $nid}) RETURN n", nid=str(node.id)
    )
    record = await result.single()
    assert record is not None
    assert record["n"]["title"] == "Graph Node"


async def test_merge_and_delete_edge(db, neo4j_session, make_user, make_node):
    owner = await make_user(email="gs_e@test.com")
    n1 = await make_node(owner, title="A")
    n2 = await make_node(owner, title="B")
    await db.commit()
    await gs.upsert_vertex(n1)
    await gs.upsert_vertex(n2)
    await gs.merge_edge(n1.id, n2.id, "LINKS_TO", created_by=str(owner.id))
    hood = await gs.get_neighborhood(
        db, n1.id, Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset()), hops=1
    )
    edge_targets = [e["target"] for e in hood["edges"]]
    assert str(n2.id) in edge_targets
    await gs.delete_edge(n1.id, n2.id, "LINKS_TO")
    hood2 = await gs.get_neighborhood(
        db, n1.id, Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset()), hops=1
    )
    assert str(n2.id) not in [e["target"] for e in hood2["edges"]]


async def test_neighborhood_visibility(db, neo4j_session, make_user, make_node):
    """Private nodes must not appear in another user's neighborhood traversal."""
    owner = await make_user(email="gs_vis1@test.com")
    other = await make_user(email="gs_vis2@test.com")
    public_node = await make_node(owner, title="Public", visibility=Visibility.public)
    private_node = await make_node(owner, title="Private", visibility=Visibility.private)
    await db.commit()
    await gs.upsert_vertex(public_node)
    await gs.upsert_vertex(private_node)
    await gs.merge_edge(public_node.id, private_node.id, "LINKS_TO", created_by="system")

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    hood = await gs.get_neighborhood(db, public_node.id, viewer, hops=1)
    node_ids = [v["id"] for v in hood["nodes"]]
    assert str(private_node.id) not in node_ids, (
        "Private node must not leak through graph traversal"
    )
