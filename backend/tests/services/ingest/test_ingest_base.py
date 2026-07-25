"""KnowledgeIngestor: idempotent upsert + two-pass edge resolution.

[plan-fix] vs the Task 2 plan block:
- test_two_pass_edge_resolution gets the neo4j_session fixture (skips when the
  graph is down, precedent: test_wikilink_extraction) and runs
  ns.run_pending_graph_ops(db) first — the ingestor QUEUES graph ops
  (PG-first, ADR-011) instead of awaiting Neo4j in-transaction.
- Recorder-based tests added (pattern from tests/services/test_node_service.py)
  so two-pass ordering and dangling-ref handling stay verified without a live
  Neo4j (kb-ingestion-connectors: dangling-link handling is a mandatory test).
"""

import pytest

from app.models.user import Role
from app.services import node_service as ns
from app.services.ingest.base import EdgeSpec, IngestItem, KnowledgeIngestor
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_upsert_new_node(db, make_user):
    owner = await make_user(email="ing_base@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    item = IngestItem(
        source="md_upload",
        source_ref="test/note.md",
        title="Test Note",
        body="# Hello\n\nThis is a test.",
        node_type="note",
        tags=["test"],
    )
    node = await ingestor.upsert(item)
    assert node.id is not None
    assert node.source == "md_upload"
    assert node.source_ref == "test/note.md"


async def test_upsert_idempotent(db, make_user):
    """Upserting the same source_ref twice returns the same node ID."""
    owner = await make_user(email="ing_idem@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    item = IngestItem(
        source="md_upload", source_ref="idem/test.md", title="Idempotent", body="body"
    )
    n1 = await ingestor.upsert(item)
    n2 = await ingestor.upsert(item)
    assert n1.id == n2.id


async def test_two_pass_edge_resolution(db, neo4j_session, make_user):
    """Edges with forward references are resolved after all nodes are ingested."""
    owner = await make_user(email="ing_edge@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    n1 = await ingestor.upsert(
        IngestItem(source="md", source_ref="a.md", title="A", body="see [[B]]")
    )
    n2 = await ingestor.upsert(
        IngestItem(source="md", source_ref="b.md", title="B", body="content")
    )

    ingestor.add_edge_spec(EdgeSpec(source_ref="a.md", target_ref="b.md", label="LINKS_TO"))
    await ingestor.resolve_edges()
    # The ingestor only QUEUES graph ops (PG-first, ADR-011); run them as the
    # post-commit caller would.
    await ns.run_pending_graph_ops(db)

    from app.services import graph_service as gs

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    targets = [e["target"] for e in hood["edges"]]
    assert str(n2.id) in targets


# --- PG-first invariant + dangling refs, verified via recorder (no live Neo4j) ---


def _graph_recorder(monkeypatch):
    """Patch graph_service functions with recorders; return the call log."""
    from app.services import graph_service as gs

    calls: list[tuple[str, ...]] = []

    async def fake_upsert(node):
        calls.append(("upsert", str(node.id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        calls.append(("edge", str(source_id), str(target_id), label, created_by, score))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)
    return calls


async def test_resolve_edges_defers_graph_sync(db, make_user, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_defer@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    n1 = await ingestor.upsert(IngestItem(source="md", source_ref="x.md", title="X", body="x"))
    n2 = await ingestor.upsert(IngestItem(source="md", source_ref="y.md", title="Y", body="y"))
    ingestor.add_edge_spec(EdgeSpec(source_ref="x.md", target_ref="y.md", label="LINKS_TO"))
    await ingestor.resolve_edges()

    assert calls == []  # nothing hit Neo4j inside the transaction
    await ns.run_pending_graph_ops(db)
    assert ("edge", str(n1.id), str(n2.id), "LINKS_TO", "ingest") in [c[:5] for c in calls]


async def test_resolve_edges_skips_dangling_refs(db, make_user, monkeypatch):
    """Unresolvable refs are skipped silently, never raised (kb-ingestion-connectors)."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_dangle@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    await ingestor.upsert(IngestItem(source="md", source_ref="only.md", title="Only", body="o"))
    ingestor.add_edge_spec(
        EdgeSpec(source_ref="only.md", target_ref="missing.md", label="LINKS_TO")
    )
    await ingestor.resolve_edges()
    await ns.run_pending_graph_ops(db)

    assert all(c[0] != "edge" for c in calls)


# --- Task 4a: DB-fallback resolution + upsert stats ---


async def test_resolve_edges_db_fallback_resolves_committed_ref(db, make_user, monkeypatch):
    """A ref absent from this batch resolves against an already-persisted row."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_fb@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    first = KnowledgeIngestor(db, viewer)
    tgt = await first.upsert(
        IngestItem(source="codebase", source_ref="r/a.py#a.beta", title="beta", body="b")
    )
    await db.flush()

    second = KnowledgeIngestor(db, viewer)  # fresh: empty _ref_to_node
    src = await second.upsert(
        IngestItem(source="codebase", source_ref="r/b.py#b.alpha", title="alpha", body="a")
    )
    second.add_edge_spec(
        EdgeSpec(
            source_ref="r/b.py#b.alpha",
            target_ref="r/a.py#a.beta",
            label="CALLS",
            props={"score": 0.7},
        )
    )
    dangling = await second.resolve_edges(db_fallback=True, fallback_source="codebase")
    assert dangling == 0
    await ns.run_pending_graph_ops(db)
    assert ("edge", str(src.id), str(tgt.id), "CALLS", "ingest", 0.7) in calls


async def test_resolve_edges_db_fallback_never_crosses_owners(db, make_user, monkeypatch):
    """Fallback is pinned to (viewer visibility, owner) — another user's node never resolves."""
    _graph_recorder(monkeypatch)
    other = await make_user(email="ing_fb_other@test.com")
    other_viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    await KnowledgeIngestor(db, other_viewer).upsert(
        IngestItem(source="codebase", source_ref="shared.py#x", title="x", body="x")
    )
    await db.flush()

    owner = await make_user(email="ing_fb_me@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ing = KnowledgeIngestor(db, viewer)
    await ing.upsert(IngestItem(source="codebase", source_ref="mine.py#m", title="m", body="m"))
    ing.add_edge_spec(EdgeSpec(source_ref="mine.py#m", target_ref="shared.py#x", label="CALLS"))
    dangling = await ing.resolve_edges(db_fallback=True, fallback_source="codebase")
    assert dangling == 1  # dangling, not another owner's node


# --- 4.R.1: DB fallback is source-scoped (source_ref unique WITHIN a source) ---


async def test_resolve_edges_db_fallback_is_source_scoped(db, make_user, monkeypatch):
    """A same-owner ref collision across sources must not mislink: the probe is
    pinned to fallback_source, so a foreign-source row counts as dangling."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_fb_src@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await KnowledgeIngestor(db, viewer).upsert(
        IngestItem(source="md_upload", source_ref="X", title="doc", body="d")
    )
    await db.flush()

    ing = KnowledgeIngestor(db, viewer)
    await ing.upsert(IngestItem(source="codebase", source_ref="c.py#f", title="f", body="f"))
    ing.add_edge_spec(EdgeSpec(source_ref="c.py#f", target_ref="X", label="CALLS"))
    assert await ing.resolve_edges(db_fallback=True, fallback_source="codebase") == 1
    await ns.run_pending_graph_ops(db)
    assert all(c[0] != "edge" for c in calls)


async def test_resolve_edges_db_fallback_matching_source_resolves(db, make_user, monkeypatch):
    """Same-owner ref collision setup, matching fallback_source: resolves to it."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_fb_src_ok@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    tgt = await KnowledgeIngestor(db, viewer).upsert(
        IngestItem(source="md_upload", source_ref="X", title="doc", body="d")
    )
    await db.flush()

    ing = KnowledgeIngestor(db, viewer)
    src = await ing.upsert(IngestItem(source="md_upload", source_ref="y.md", title="y", body="y"))
    ing.add_edge_spec(EdgeSpec(source_ref="y.md", target_ref="X", label="LINKS_TO"))
    assert await ing.resolve_edges(db_fallback=True, fallback_source="md_upload") == 0
    await ns.run_pending_graph_ops(db)
    assert ("edge", str(src.id), str(tgt.id), "LINKS_TO", "ingest") in [c[:5] for c in calls]


async def test_resolve_edges_db_fallback_without_source_never_probes(db, make_user, monkeypatch):
    """db_fallback with fallback_source=None SKIPS the DB probe (dangling) —
    an unscoped probe could hit any source's ref (never probe unscoped)."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_fb_nosrc@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await KnowledgeIngestor(db, viewer).upsert(
        IngestItem(source="codebase", source_ref="z.py#z", title="z", body="z")
    )
    await db.flush()

    ing = KnowledgeIngestor(db, viewer)
    await ing.upsert(IngestItem(source="codebase", source_ref="w.py#w", title="w", body="w"))
    ing.add_edge_spec(EdgeSpec(source_ref="w.py#w", target_ref="z.py#z", label="CALLS"))
    assert await ing.resolve_edges(db_fallback=True) == 1
    await ns.run_pending_graph_ops(db)
    assert all(c[0] != "edge" for c in calls)


async def test_upsert_stats_created_updated_skipped(db, make_user):
    owner = await make_user(email="ing_stats@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ing = KnowledgeIngestor(db, viewer)
    await ing.upsert(IngestItem(source="codebase", source_ref="s.py", title="S", body="1"))
    await ing.upsert(IngestItem(source="codebase", source_ref="s.py", title="S", body="1"))
    await ing.upsert(IngestItem(source="codebase", source_ref="s.py", title="S", body="2"))
    assert ing.stats == {"created": 1, "skipped": 1, "updated": 1}
