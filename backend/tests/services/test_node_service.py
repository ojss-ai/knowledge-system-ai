import pytest

from app.models.user import Role, Visibility
from app.services import node_service as ns
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_create_node(db, make_user):
    owner = await make_user(email="ns_create@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    node = await ns.create_node(db, viewer=viewer, title="My Note", body="hello", node_type="note")
    assert node.id is not None
    assert node.owner_id == owner.id
    assert node.title == "My Note"


async def test_get_node_own(db, make_user, make_node):
    owner = await make_user(email="ns_get@test.com")
    node = await make_node(owner, title="GetMe")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    result = await ns.get_node(db, node.id, viewer)
    assert result.id == node.id


async def test_get_node_forbidden(db, make_user, make_node):
    from app.core.errors import ForbiddenError

    owner = await make_user(email="ns_fo@test.com")
    other = await make_user(email="ns_fo2@test.com")
    node = await make_node(owner, visibility=Visibility.private)
    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    with pytest.raises(ForbiddenError):
        await ns.get_node(db, node.id, viewer)


async def test_update_node_creates_revision(db, make_user, make_node):
    owner = await make_user(email="ns_upd@test.com")
    node = await make_node(owner, title="Old Title", body="old body")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    updated = await ns.update_node(db, node.id, viewer, title="New Title", body="new body")
    assert updated.title == "New Title"
    await db.refresh(node, ["revisions"])
    assert len(node.revisions) == 1
    assert node.revisions[0].title_snapshot == "Old Title"


async def test_wikilink_extraction(db, neo4j_session, make_user, make_node):
    # [plan-fix] neo4j_session fixture added: this test verifies edges via live
    # Neo4j (get_neighborhood), so it must skip when Neo4j is unreachable.
    owner = await make_user(email="ns_wl@test.com")
    n1 = await make_node(owner, title="Source Note", body="see [[Target Note]] and [[Other]]")
    n2 = await make_node(owner, title="Target Note", body="")
    await db.flush()
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.resolve_wikilinks(db, n1, viewer)
    # [plan-fix] resolve_wikilinks only QUEUES graph ops (PG-first, ADR-011);
    # run them as the post-commit caller would.
    await ns.run_pending_graph_ops(db)
    # edges should have been created — verify via graph service
    from app.services import graph_service as gs

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    targets = [e["target"] for e in hood["edges"]]
    assert str(n2.id) in targets


async def test_soft_delete(db, make_user, make_node):
    from app.core.errors import NotFoundError

    owner = await make_user(email="ns_del@test.com")
    node = await make_node(owner)
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.delete_node(db, node.id, viewer)
    with pytest.raises(NotFoundError):
        await ns.get_node(db, node.id, viewer)


# --- PG-first invariant: no Neo4j work inside the transaction (ADR-011) ---


def _graph_recorder(monkeypatch):
    """Patch graph_service functions with recorders; return the call log."""
    from app.services import graph_service as gs

    calls: list[tuple[str, ...]] = []

    async def fake_upsert(node):
        calls.append(("upsert", str(node.id)))

    async def fake_soft_delete(node_id):
        calls.append(("soft_delete", str(node_id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        calls.append(("edge", str(source_id), str(target_id), label))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "soft_delete_vertex", fake_soft_delete)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)
    return calls


async def test_create_node_queues_graph_op(db, make_user, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ns_q_create@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    node = await ns.create_node(db, viewer=viewer, title="Queued")
    assert calls == []  # nothing hit Neo4j inside the transaction
    assert len(ns.pending_graph_ops(db)) == 1
    await ns.run_pending_graph_ops(db)
    assert calls == [("upsert", str(node.id))]
    assert ns.pending_graph_ops(db) == []


async def test_update_node_defers_graph_sync(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ns_q_upd@test.com")
    node = await make_node(owner, title="Sync Later")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.update_node(db, node.id, viewer, title="Synced")
    assert calls == []  # graph op queued, not executed, pre-commit
    assert len(ns.pending_graph_ops(db)) == 1
    await ns.run_pending_graph_ops(db)
    assert calls == [("upsert", str(node.id))]


async def test_delete_node_defers_graph_sync(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ns_q_del@test.com")
    node = await make_node(owner)
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.delete_node(db, node.id, viewer)
    assert calls == []
    assert len(ns.pending_graph_ops(db)) == 1
    await ns.run_pending_graph_ops(db)
    assert calls == [("soft_delete", str(node.id))]


async def test_resolve_wikilinks_defers_graph_sync(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ns_q_wl@test.com")
    n1 = await make_node(owner, title="WL Source", body="see [[WL Target]]")
    n2 = await make_node(owner, title="WL Target", body="")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.resolve_wikilinks(db, n1, viewer)
    assert calls == []  # edges queued for post-commit, not merged in-transaction
    assert len(ns.pending_graph_ops(db)) == 3  # upsert n1, upsert n2, edge
    await ns.run_pending_graph_ops(db)
    assert ("edge", str(n1.id), str(n2.id), "LINKS_TO") in calls


async def test_resolve_wikilinks_skips_self_link(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ns_self_wl@test.com")
    node = await make_node(owner, title="Self Note", body="loop [[Self Note]]")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.resolve_wikilinks(db, node, viewer)
    await ns.run_pending_graph_ops(db)
    assert all(c[0] != "edge" for c in calls)  # [[Own Title]] must not self-link
