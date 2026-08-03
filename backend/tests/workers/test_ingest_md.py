"""Ingest Celery task: idempotent zip ingestion + IngestionRun tracking.

[plan-fix] vs the Task 4 plan block (carried over from Task 2):
- The ingest contract QUEUES all graph ops on the session (PG-first, ADR-011);
  the plan block never drained them. Recorder tests below prove vertices/edges
  only flow post-commit.
- Under the real task_session, an exception rolls back the impl's in-transaction
  `status=failed` write; `_run_ingest` re-marks the run failed in a fresh
  transaction so retries/Task-5 status readers see it.

[review-fix 4.R]:
- Per-item durability (kb-celery-jobs rule 5): `_run_ingest` checkpoints
  (commit + drain) every item, so a mid-zip failure keeps the nodes AND the
  processed_items counter committed so far — resumable via content-hash skip.
- Graph ops drain batch-by-batch INSIDE the open task_session, never after it
  closed; the fake task_session now exercises real close semantics.
"""

import io
import uuid
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingest import IngestionRun, RunStatus
from app.models.knowledge import KnowledgeNode
from app.models.user import Role
from app.services import node_service as ns
from app.services.ingest.base import KnowledgeIngestor
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
    """[review-fix 4.R] Mirror REAL task_session semantics on the test
    connection: a SEPARATE AsyncSession whose commits release savepoints on the
    test's outer transaction (join_transaction_mode="create_savepoint", so
    'durable' work still rolls back with the test), a logged ("commit",) marker
    per commit, commit-on-clean-exit, and a REAL close (("close",) marker).

    The previous fake yielded the long-lived test session inside one savepoint:
    it could not represent mid-block batch commits and kept the session
    artificially open after the context exited — hiding that the worker drained
    graph ops on an already-closed session."""

    @asynccontextmanager
    async def fake():
        conn = await db.connection()
        inner = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        real_commit = inner.commit

        async def logged_commit() -> None:
            await real_commit()
            log.append(("commit",))

        inner.commit = logged_commit  # type: ignore[method-assign]
        try:
            yield inner
            await inner.commit()  # the real task_session commits on clean exit
        finally:
            await inner.close()
            log.append(("close",))

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
    """[review-fix 4.R] worker orchestration: graph ops flow batch-by-batch —
    each drain AFTER its batch's commit and BEFORE the task session closes."""
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

    commit_idxs = [i for i, e in enumerate(log) if e == ("commit",)]
    close_idx = log.index(("close",))
    graph_idxs = [i for i, e in enumerate(log) if e[0] in ("vertex", "edge")]
    assert graph_idxs, "the worker must drain the queued graph ops"
    assert all(i > min(commit_idxs) for i in graph_idxs), (
        "graph ops ran before the first commit boundary (ADR-011 violation)"
    )
    assert all(i < close_idx for i in graph_idxs), (
        "graph ops must drain while the task session is still open, not after close"
    )
    assert any(i < max(commit_idxs) for i in graph_idxs), (
        "graph ops must flow batch-by-batch, not lumped after the final commit"
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
    run_id = (await _make_run(db, owner)).id

    with pytest.raises(zipfile.BadZipFile):
        await _run_ingest(run_id, b"not a zip", viewer)

    db.expire_all()  # the fake task session updated the row behind this session
    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert all(e[0] not in ("vertex", "edge") for e in log), (
        "graph ops from a rolled-back transaction must never run"
    )


async def test_run_ingest_failure_mid_zip_keeps_committed_progress(db, make_user, monkeypatch):
    """[review-fix 4.R, kb-celery-jobs rule 5] per-item durability: items
    committed before a mid-zip failure persist — their nodes AND the
    processed_items counter — and the run is marked failed WITH the accumulated
    counts. A re-run of the same zip then converges idempotently (content-hash
    skip), making the job resumable."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    monkeypatch.setattr(ingest_md_module, "task_session", _fake_task_session(db, log))

    real_upsert = KnowledgeIngestor.upsert

    async def flaky_upsert(self, item):
        if item.source_ref == "b.md":
            raise RuntimeError("injected failure on item 2")
        return await real_upsert(self, item)

    monkeypatch.setattr(KnowledgeIngestor, "upsert", flaky_upsert)

    owner = await make_user(email="imd_midzip@test.com")
    owner_id = owner.id  # captured: expire_all() below makes attribute access lazy-load
    viewer = Viewer(user_id=owner_id, role=Role.user, group_ids=frozenset())
    run_id = (await _make_run(db, owner)).id

    zip_bytes = make_zip(
        {
            "a.md": "# A\n\nfirst",
            "b.md": "# B\n\nsecond",
            "c.md": "# C\n\nthird",
        }
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        await _run_ingest(run_id, zip_bytes, viewer)

    db.expire_all()
    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert run_result.processed_items == 1, (
        "counters committed before the failure must survive it (rule 5)"
    )

    titles = (
        await db.scalars(
            select(KnowledgeNode.title).where(
                KnowledgeNode.owner_id == owner_id, KnowledgeNode.source == "md_upload"
            )
        )
    ).all()
    assert titles == ["A"], "nodes committed before the failure must persist"

    # Resumable: re-running the (now healthy) zip converges without duplicates.
    monkeypatch.setattr(KnowledgeIngestor, "upsert", real_upsert)
    await db.refresh(owner)  # expired above; _make_run reads owner.id
    run2_id = (await _make_run(db, owner)).id
    await _run_ingest(run2_id, zip_bytes, viewer)

    db.expire_all()
    count = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner_id, KnowledgeNode.source == "md_upload")
    )
    assert count == 3, "re-run must converge: A skipped by content hash, B and C created"
    run2_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run2_id))
    assert run2_result.status == RunStatus.done
    assert run2_result.processed_items == 3


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
