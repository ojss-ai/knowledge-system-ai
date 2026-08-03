"""Celery task: idempotent Markdown zip ingestion with IngestionRun tracking.

PG first, Neo4j second (ADR-011): _ingest_md_impl only QUEUES graph ops (via
KnowledgeIngestor / node_service); the per-batch checkpoint drains them with
run_pending_graph_ops right AFTER each commit, while the session is still open.

Durability ([review-fix 4.R], kb-celery-jobs rule 5): the run commits every
_COMMIT_EVERY items, so nodes and the processed_items counter survive a
mid-zip failure — the counts Task 5 status readers see are real, and a re-run
converges from where the last commit left off (content-hash skip): resumable.

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

# Durability checkpoint: commit accumulated work, then drain queued graph ops.
Checkpoint = Callable[[AsyncSession], Awaitable[None]]

# Items per durability commit (kb-celery-jobs rule 5). 1 because items are
# whole Markdown files (coarse units) and the Task 5 WS reader polls
# processed_items — a bigger batch would freeze visible progress and lose more
# work on a crash. Raise only with evidence that commit overhead matters.
_COMMIT_EVERY = 1


async def _checkpoint(db: AsyncSession) -> None:
    """[review-fix 4.R] Durability boundary: commit the accumulated batch, then
    drain the queued graph ops POST-commit while the session is still open
    (ADR-011 + kb-celery-jobs rule 5). Neo4j stays best-effort — each op is
    wrapped in _graph_sync, so a graph failure never undoes the committed
    batch (a Celery retry re-converges the graph)."""
    await db.commit()
    await ns.run_pending_graph_ops(db)


async def _ingest_md_impl(
    db: AsyncSession,
    run_id: uuid.UUID,
    zip_bytes: bytes,
    viewer: Viewer,
    progress_callback: ProgressCallback | None = None,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Core logic, testable without a broker. Graph ops are only queued here,
    never awaited (ADR-011). `checkpoint` (commit + post-commit drain, provided
    by _run_ingest) runs every _COMMIT_EVERY items and once after edge
    resolution; without one (in-transaction unit tests) work is only flushed
    and stays inside the caller's transaction."""
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
            # Durability boundary (rule 5): everything up to and including
            # this item — nodes, counters, queued vertex syncs — becomes
            # permanent now, so a later failure cannot take it back.
            if checkpoint is not None and (i + 1) % _COMMIT_EVERY == 0:
                await checkpoint(db)
            else:
                await db.flush()
            if progress_callback:
                await progress_callback(i + 1, len(items))

        await ingestor.resolve_edges()

        run.status = RunStatus.done
        run.finished_at = datetime.now(UTC)
        # Final checkpoint: the tail batch, the resolved edges and the done
        # status must be durable and drained BEFORE the session closes.
        if checkpoint is not None:
            await checkpoint(db)
        else:
            await db.flush()

    except Exception as exc:
        # Best effort for callers running everything in one transaction (unit
        # tests). Under _run_ingest this uncommitted write is discarded on
        # close and _mark_run_failed re-marks the run in a fresh session —
        # KEEPING the counters committed by earlier checkpoints.
        run.status = RunStatus.failed
        run.error_log = str(exc)
        run.finished_at = datetime.now(UTC)
        await db.flush()
        raise


async def _mark_run_failed(db: AsyncSession, run_id: uuid.UUID, error: str) -> None:
    """[plan-fix] persist the failure OUTSIDE the rolled-back ingest transaction,
    so Celery retries and Task 5 status readers see status=failed. Touches
    status/error/finished_at ONLY: processed_items keeps the counts accumulated
    by the committed batches ([review-fix 4.R] — 'failed after N of M')."""
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None:
        return
    run.status = RunStatus.failed
    run.error_log = error
    run.finished_at = datetime.now(UTC)
    await db.flush()


async def _run_ingest(run_id: uuid.UUID, zip_bytes: bytes, viewer: Viewer) -> None:
    """Session orchestration for the task. The impl checkpoints (commit + drain)
    batch-by-batch INSIDE the open task_session — nothing graph-related runs
    after the session closes ([review-fix 4.R]: the first cut drained once
    after the context exited, which lumped every graph op at the end and only
    worked because expire_on_commit=False left the closed session readable).
    On failure the current batch rolls back with the session, committed batches
    (nodes + counters) persist, and the run is re-marked failed in a fresh
    session before the error propagates for retry."""
    try:
        async with task_session() as db:
            await _ingest_md_impl(db, run_id, zip_bytes, viewer, checkpoint=_checkpoint)
    except Exception as exc:
        async with task_session() as fail_db:
            await _mark_run_failed(fail_db, run_id, str(exc))
        raise


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
