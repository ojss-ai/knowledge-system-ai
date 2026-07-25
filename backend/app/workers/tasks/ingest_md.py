"""Celery task: idempotent Markdown zip ingestion with IngestionRun tracking.

PG first, Neo4j second (ADR-011): _ingest_md_impl runs inside the caller's
transaction and only QUEUES graph ops (via KnowledgeIngestor / node_service);
_run_ingest drains them with run_pending_graph_ops AFTER task_session commits.

Idempotency (kb-celery-jobs rule 1): KnowledgeIngestor.upsert is keyed on
(owner_id, source, source_ref) with a content-hash short-circuit, so re-running
the task on the same zip creates zero new nodes.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from celery import Task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingest import IngestionRun, RunStatus
from app.models.user import Role
from app.services import node_service as ns
from app.services.ingest.base import KnowledgeIngestor
from app.services.ingest.md_importer import parse_zip
from app.services.visibility import Viewer
from app.workers.celery_app import celery_app, task_session

# Progress hook (processed, total) — Task 5 wires this to Redis pub/sub
# channel run:{ingestion_run_id} (kb-celery-jobs rule 8).
ProgressCallback = Callable[[int, int], Awaitable[None]]


async def _ingest_md_impl(
    db: AsyncSession,
    run_id: uuid.UUID,
    zip_bytes: bytes,
    viewer: Viewer,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Core logic, testable without a broker. Runs INSIDE the caller's
    transaction: graph ops are only queued here, never awaited (ADR-011)."""
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None:
        return

    run.status = RunStatus.running
    await db.flush()

    try:
        items, edge_specs = parse_zip(zip_bytes, source="md_upload")
        run.total_items = len(items)
        await db.flush()

        ingestor = KnowledgeIngestor(db, viewer)

        for i, item in enumerate(items):
            await ingestor.upsert(item)
            for spec in edge_specs:
                if spec.source_ref == item.source_ref:
                    ingestor.add_edge_spec(spec)
            run.processed_items = i + 1
            await db.flush()
            if progress_callback:
                await progress_callback(i + 1, len(items))

        await ingestor.resolve_edges()

        run.status = RunStatus.done
        run.finished_at = datetime.now(UTC)
        await db.flush()

    except Exception as exc:
        # In-transaction best effort only: under task_session the re-raise
        # rolls this back too — _run_ingest re-marks the run in a fresh tx.
        run.status = RunStatus.failed
        run.error_log = str(exc)
        run.finished_at = datetime.now(UTC)
        await db.flush()
        raise


async def _mark_run_failed(db: AsyncSession, run_id: uuid.UUID, error: str) -> None:
    """[plan-fix] persist the failure OUTSIDE the rolled-back ingest transaction,
    so Celery retries and Task 5 status readers see status=failed."""
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None:
        return
    run.status = RunStatus.failed
    run.error_log = error
    run.finished_at = datetime.now(UTC)
    await db.flush()


async def _run_ingest(run_id: uuid.UUID, zip_bytes: bytes, viewer: Viewer) -> None:
    """Session orchestration for the task: impl inside task_session (commits on
    exit), then drain the queued Neo4j ops POST-commit (ADR-011, [plan-fix] —
    the plan block never drained them). On failure nothing is drained (the ops
    belong to a rolled-back transaction) and the run is marked failed in a
    fresh transaction before the error propagates for retry."""
    try:
        async with task_session() as db:
            await _ingest_md_impl(db, run_id, zip_bytes, viewer)
    except Exception as exc:
        async with task_session() as fail_db:
            await _mark_run_failed(fail_db, run_id, str(exc))
        raise
    await ns.run_pending_graph_ops(db)


@celery_app.task(  # type: ignore[untyped-decorator]  # celery is untyped (ignore_missing_imports)
    bind=True,
    name="kb.ingest_md",
    queue="ingest",  # long-running batch work (kb-celery-jobs rule 6); workers consume -Q ingest
    acks_late=True,
    max_retries=2,
    retry_backoff=True,
)
def ingest_md(
    self: Task, run_id: str, zip_b64: str, user_id: str, role: str, group_ids: list[str]
) -> None:
    """Celery task: ingest a zip of Markdown files for the given viewer.
    Args are primitives only (kb-celery-jobs rule 2); the zip travels base64.
    Idempotent — safe to re-run on at-least-once delivery."""
    zip_bytes = base64.b64decode(zip_b64)
    viewer = Viewer(
        user_id=uuid.UUID(user_id),
        role=Role(role),
        group_ids=frozenset(uuid.UUID(g) for g in group_ids),
    )

    try:
        asyncio.run(_run_ingest(uuid.UUID(run_id), zip_bytes, viewer))
    except Exception as exc:
        raise self.retry(exc=exc) from exc
