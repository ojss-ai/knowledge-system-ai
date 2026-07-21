import math
import uuid

import pytest

from app.models.chunk import NodeChunk
from app.models.user import Role, Visibility
from app.services.visibility import Viewer
from app.workers.tasks.autolink_node import _autolink_node_impl, autolink_node
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


# [plan-fix] The plan's two tests verified edges via gs.get_neighborhood, which
# needs live Neo4j; they are kept below (neo4j-marked). The pure-PG logic
# (similarity query, threshold, top-K, candidate visibility) is tested here with
# the graph-recorder pattern from tests/services/test_node_service.py so it runs
# without Neo4j.


def _graph_recorder(monkeypatch):
    """Patch graph_service functions with recorders; return the call log."""
    from app.services import graph_service as gs

    calls: list[tuple[str, ...]] = []

    async def fake_upsert(node):
        calls.append(("upsert", str(node.id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        calls.append(("edge", str(source_id), str(target_id), label, created_by))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)
    return calls


def _vec_with_cosine(cos_to_e1: float) -> list[float]:
    """Unit vector whose cosine to e1 = [1, 0, ...] is exactly cos_to_e1."""
    v = [0.0] * 768
    v[0] = cos_to_e1
    v[1] = math.sqrt(1.0 - cos_to_e1**2)
    return v


async def _add_chunk(db, node, vec: list[float]) -> None:
    db.add(
        NodeChunk(id=uuid.uuid4(), node_id=node.id, chunk_index=0, chunk_text="t", embedding=vec)
    )
    await db.flush()


def _viewer(user) -> Viewer:
    return Viewer(user_id=user.id, role=Role.user, group_ids=frozenset())


async def test_autolink_task_options():
    """Canonical kb-celery-jobs task shape: default queue, backoff retries, late acks."""
    assert autolink_node.queue == "default", "autolink is light DB/graph I/O -> default queue"
    assert autolink_node.retry_backoff is True
    assert autolink_node.acks_late is True
    assert autolink_node.max_retries == 3


async def test_autolink_creates_similar_to_edge(
    db, make_user, make_node, fake_embedder, monkeypatch
):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_rec1@test.com")
    # FakeEmbedder is deterministic — same text = identical vector = cosine 1.0
    n1 = await make_node(
        owner,
        title="Python Tips",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    n2 = await make_node(
        owner,
        title="Python Guide",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    await _autolink_node_impl(db, n1.id, _viewer(owner))

    # one SIMILAR_TO per pair, lower node id as source (kb-pgvector-search)
    src, tgt = sorted((n1.id, n2.id))
    assert ("edge", str(src), str(tgt), "SIMILAR_TO", "system:autolink") in calls


async def test_autolink_idempotent(db, make_user, make_node, fake_embedder, monkeypatch):
    """Re-running autolink issues the exact same MERGE calls — no new/extra edges."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_rec2@test.com")
    n1 = await make_node(
        owner, title="Topic A", body="Same content here.", visibility=Visibility.public
    )
    n2 = await make_node(
        owner, title="Topic B", body="Same content here.", visibility=Visibility.public
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)
    first_run = list(calls)
    edges = [c for c in first_run if c[0] == "edge"]
    assert len(edges) == len(set(edges)) == 1, "exactly one MERGE per pair, once"

    calls.clear()
    await _autolink_node_impl(db, n1.id, viewer)  # second run
    assert calls == first_run, "second run must repeat identical MERGEs (idempotent)"


async def test_autolink_respects_cosine_threshold(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_thr@test.com")
    src = await make_node(owner, title="Src", visibility=Visibility.public)
    near = await make_node(owner, title="Near", visibility=Visibility.public)
    far = await make_node(owner, title="Far", visibility=Visibility.public)
    await _add_chunk(db, src, _vec_with_cosine(1.0))
    await _add_chunk(db, near, _vec_with_cosine(0.9))  # >= 0.82 -> linked
    await _add_chunk(db, far, _vec_with_cosine(0.5))  # < 0.82 -> not linked

    await _autolink_node_impl(db, src.id, _viewer(owner))

    linked = {c[2] for c in calls if c[0] == "edge"} | {c[1] for c in calls if c[0] == "edge"}
    assert str(near.id) in linked
    assert str(far.id) not in linked


async def test_autolink_caps_at_top_k(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_topk@test.com")
    src = await make_node(owner, title="Src", visibility=Visibility.public)
    await _add_chunk(db, src, _vec_with_cosine(1.0))

    cosines = [0.99, 0.97, 0.95, 0.93, 0.91, 0.89, 0.85]  # 7 candidates over threshold
    nodes = []
    for i, c in enumerate(cosines):
        n = await make_node(owner, title=f"Cand {i}", visibility=Visibility.public)
        await _add_chunk(db, n, _vec_with_cosine(c))
        nodes.append(n)

    await _autolink_node_impl(db, src.id, _viewer(owner))

    edges = [c for c in calls if c[0] == "edge"]
    assert len(edges) == 5, "top-K is 5"
    linked = {e[1] for e in edges} | {e[2] for e in edges}
    for n in nodes[:5]:
        assert str(n.id) in linked, "the 5 MOST similar candidates must win"
    for n in nodes[5:]:
        assert str(n.id) not in linked


async def test_autolink_excludes_other_users_private_nodes(
    db, make_user, make_node, fake_embedder, monkeypatch
):
    """Candidate set uses the OWNER's viewer: another user's private node must
    never be auto-linked, even at cosine 1.0 (existence must not leak)."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_vis1@test.com")
    other = await make_user(email="al_vis2@test.com")
    mine = await make_node(
        owner, title="Mine", body="identical secret content", visibility=Visibility.public
    )
    secret = await make_node(
        other, title="Secret", body="identical secret content", visibility=Visibility.private
    )
    await db.flush()

    await _embed_node_impl(db, mine.id, fake_embedder)
    await _embed_node_impl(db, secret.id, fake_embedder)

    await _autolink_node_impl(db, mine.id, _viewer(owner))

    assert all(str(secret.id) not in c for c in calls), "private node leaked into autolink"


async def test_autolink_without_chunks_is_noop(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_noop@test.com")
    node = await make_node(owner, title="Unembedded", visibility=Visibility.public)
    await db.flush()

    await _autolink_node_impl(db, node.id, _viewer(owner))
    assert calls == []


# --- Plan's original graph-level tests: need live Neo4j (skip when unreachable) ---


async def test_autolink_creates_similar_to_edges(
    db, neo4j_session, make_user, make_node, fake_embedder
):
    from app.services import graph_service as gs

    owner = await make_user(email="al1@test.com")
    n1 = await make_node(
        owner,
        title="Python Tips",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    n2 = await make_node(
        owner,
        title="Python Guide",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    edge_labels = [e["label"] for e in hood["edges"]]
    assert "SIMILAR_TO" in edge_labels


async def test_autolink_idempotent_in_graph(db, neo4j_session, make_user, make_node, fake_embedder):
    """Running autolink twice must not create duplicate SIMILAR_TO edges."""
    from app.services import graph_service as gs

    owner = await make_user(email="al2@test.com")
    n1 = await make_node(
        owner, title="Topic A", body="Same content here.", visibility=Visibility.public
    )
    n2 = await make_node(
        owner, title="Topic B", body="Same content here.", visibility=Visibility.public
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)
    await _autolink_node_impl(db, n1.id, viewer)  # second run

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    similar_edges = [e for e in hood["edges"] if e["label"] == "SIMILAR_TO"]
    targets = [(e["source"], e["target"]) for e in similar_edges]
    assert len(targets) == len(set(targets)), "Duplicate SIMILAR_TO edges detected"
