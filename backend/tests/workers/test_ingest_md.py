"""Ingest Celery task: idempotent zip ingestion + IngestionRun tracking.

[plan-fix] vs the Task 4 plan block (carried over from Task 2):
- The ingest contract QUEUES all graph ops on the session (PG-first, ADR-011);
  the plan block never drained them. `_run_ingest` (worker orchestration) runs
  `ns.run_pending_graph_ops(db)` AFTER task_session commits — recorder tests
  below prove vertices/edges only flow post-commit.
- Under the real task_session, an exception rolls back the impl's in-transaction
  `status=failed` write; `_run_ingest` re-marks the run failed in a fresh
  transaction so retries/Task-5 status readers see it.
"""

import io
import uuid
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from app.models.ingest import IngestionRun, RunStatus
from app.models.knowledge import KnowledgeNode
from app.models.user import Role
from app.services import node_service as ns
from app.services.visibility import Viewer
from app.workers.tasks import ingest_md as ingest_md_module
from app.workers.tasks.ingest_md import (
    _ingest_md_impl,
    _mark_run_failed,
    _run_ingest,
    ingest_md,
)

pytestmark = pytest.mark.asyncio


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


async def _make_run(db, owner, total_items: int = 1) -> IngestionRun:
    run = IngestionRun(
        id=uuid.uuid4(), owner_id=owner.id, source="md_upload", total_items=total_items
    )
    db.add(run)
    await db.flush()
    return run


async def test_ingest_md_task_options():
    """Canonical kb-celery-jobs task shape: ingest queue, backoff retries, late acks."""
    assert ingest_md.queue == "ingest", "long-running ingestion must route to the ingest queue"
    assert ingest_md.retry_backoff is True
    assert ingest_md.acks_late is True
    assert ingest_md.max_retries == 2


async def test_ingest_creates_nodes(db, make_user):
    owner = await make_user(email="imdingest1@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner, total_items=2)

    zip_bytes = make_zip(
        {
            "note1.md": "# Alpha\n\nFirst note.",
            "note2.md": "# Beta\n\nSecond note.",
        }
    )

    await _ingest_md_impl(db, run.id, zip_bytes, viewer)

    count = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )
    assert count == 2

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.done
    assert run_result.processed_items == 2
    assert run_result.finished_at is not None


async def test_ingest_idempotent(db, make_user):
    """Ingesting the same zip twice must NOT create duplicate nodes."""
    owner = await make_user(email="imd_idem@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())

    zip_bytes = make_zip({"page.md": "# Same Page\n\nContent."})

    run1 = await _make_run(db, owner)
    await _ingest_md_impl(db, run1.id, zip_bytes, viewer)

    count1 = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )

    run2 = await _make_run(db, owner)
    await _ingest_md_impl(db, run2.id, zip_bytes, viewer)

    count2 = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )
    assert count1 == count2, "Idempotency violated: duplicate nodes created"


async def test_ingest_missing_run_is_noop(db, make_user):
    """Run row gone (race): tolerate, don't crash (kb-celery-jobs task shape)."""
    owner = await make_user(email="imd_norun@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await _ingest_md_impl(db, uuid.uuid4(), make_zip({"a.md": "# A"}), viewer)

    count = await db.scalar(
        select(func.count()).select_from(KnowledgeNode).where(KnowledgeNode.owner_id == owner.id)
    )
    assert count == 0


async def test_ingest_failure_marks_run_failed_and_raises(db, make_user):
    """A bad zip must set status=failed + error_log and re-raise for Celery retry."""
    owner = await make_user(email="imd_fail@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner)

    with pytest.raises(zipfile.BadZipFile):
        await _ingest_md_impl(db, run.id, b"this is not a zip", viewer)

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert run_result.finished_at is not None


# --- [plan-fix] PG-first invariant: graph ops flow only AFTER commit (ADR-011) ---
# Recorder pattern from tests/services/ingest/test_ingest_base.py — no live Neo4j.


def _graph_recorder(monkeypatch, log: list[tuple[str, ...]]) -> None:
    from app.services import graph_service as gs

    async def fake_upsert(node):
        log.append(("vertex", str(node.id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        log.append(("edge", str(source_id), str(target_id), label))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)


def _fake_task_session(db, log: list[tuple[str, ...]]):
    """Mimic task_session on the test session: savepoint (rolled back on error,
    released on success), then a 'commit' marker — so the log shows whether
    graph ops ran before or after the commit boundary."""

    @asynccontextmanager
    async def fake():
        async with db.begin_nested():
            yield db
        log.append(("commit",))

    return fake


async def test_impl_queues_graph_ops_without_running_them(db, make_user, monkeypatch):
    """_ingest_md_impl runs in-transaction: it must only QUEUE graph ops."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    owner = await make_user(email="imd_queue@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner, total_items=2)

    zip_bytes = make_zip(
        {
            "a.md": "# A\n\nsee [[B]]",
            "b.md": "# B\n\ncontent",
        }
    )
    await _ingest_md_impl(db, run.id, zip_bytes, viewer)

    assert log == [], "nothing may hit Neo4j inside the transaction (ADR-011)"
    await ns.run_pending_graph_ops(db)
    assert any(e[0] == "vertex" for e in log)
    assert any(e[0] == "edge" and e[3] == "LINKS_TO" for e in log)


async def test_run_ingest_drains_graph_ops_post_commit(db, make_user, monkeypatch):
    """[plan-fix] worker orchestration: vertices/edges flow only AFTER commit."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    monkeypatch.setattr(ingest_md_module, "task_session", _fake_task_session(db, log))

    owner = await make_user(email="imd_drain@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner, total_items=2)

    zip_bytes = make_zip(
        {
            "x.md": "# X\n\nsee [[Y]]",
            "y.md": "# Y\n\ncontent",
        }
    )
    await _run_ingest(run.id, zip_bytes, viewer)

    commit_idx = log.index(("commit",))
    graph_idxs = [i for i, e in enumerate(log) if e[0] in ("vertex", "edge")]
    assert graph_idxs, "the worker must drain the queued graph ops"
    assert all(i > commit_idx for i in graph_idxs), (
        "graph ops ran before the commit boundary (ADR-011 violation)"
    )
    assert any(log[i][0] == "edge" for i in graph_idxs), "wikilink edge must be merged"


async def test_run_ingest_marks_run_failed_in_fresh_tx(db, make_user, monkeypatch):
    """[plan-fix] the impl's in-tx failed write rolls back with the transaction;
    _run_ingest must persist status=failed in a fresh transaction, then re-raise."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    monkeypatch.setattr(ingest_md_module, "task_session", _fake_task_session(db, log))

    owner = await make_user(email="imd_failtx@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner)

    with pytest.raises(zipfile.BadZipFile):
        await _run_ingest(run.id, b"not a zip", viewer)

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert all(e[0] not in ("vertex", "edge") for e in log), (
        "graph ops from a rolled-back transaction must never run"
    )


async def test_mark_run_failed(db, make_user):
    owner = await make_user(email="imd_mark@test.com")
    run = await _make_run(db, owner)

    await _mark_run_failed(db, run.id, "boom")

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log == "boom"
    assert run_result.finished_at is not None


async def test_mark_run_failed_missing_run_is_noop(db):
    await _mark_run_failed(db, uuid.uuid4(), "boom")  # must not raise
