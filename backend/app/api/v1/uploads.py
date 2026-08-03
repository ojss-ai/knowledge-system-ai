"""Uploads router — POST a zip of Markdown files, ingest asynchronously (Celery),
track the run, and stream progress over WebSocket.

The run-status probes here are sanctioned raw queries (daily_logs precedent):
`ingestion_runs` is upload bookkeeping owned by exactly one user, not a
knowledge read path — ownership (owner_id == viewer.user_id) is the whole
visibility rule, answered 404-generic like any invisible read.

> Scale note (Task 5 plan blockquote): `ingest_md.delay(..., base64(zip))`
> pushes up to ~133 MB per message through Redis at the 100 MB cap. Phase 7
> hardening candidate: store the upload in MinIO (`storage.upload_file`) and
> pass the object path instead of the payload. Do not redesign here.

[plan-fix] vs the Task 5.2 block:
- `get_scoped_viewer`, not `get_current_viewer`: the admin visibility bypass is
  only reachable under /api/v1/admin/* (Phase 1 standard, kb-visibility rule 5).
- `zipfile.is_zipfile` needs a file-like object; the plan passed raw bytes
  (`content.__class__(content)`), which it cannot read.
- get_run answers another user's run with a generic 404 (invisible ==
  nonexistent, get_node standard) instead of the plan's 403 — a 403 confirms
  the run id exists.
- The WS poll re-selects with populate_existing=True: the session identity map
  would otherwise pin the first-read attributes and the stream would never see
  the worker's committed progress.
- summary/operation_id added per kb-api-conventions.

[plan-fix] vs the Task 4.2 block (phase 5, ingest-item):
- `get_scoped_viewer`, not `get_current_viewer` (same Phase 1 standard as above).
- `run_pending_graph_ops(db)` after the commit: create/update queue the Neo4j
  vertex sync on the session; the plan block committed and never drained it
  (ADR-011, nodes.py standard).
- `IngestItemIn(NodeCreate)` adds the `tags` field: the sync engine (Task 3)
  sends Confluence labels as `tags` and IngestItem carries them, but NodeCreate
  has no tags field — the plan's block dropped labels silently, breaking the
  labels → tags rule (kb-ingestion-connectors).
- `NodeOut.model_validate(node)` + summary/operation_id per kb-api-conventions
  (never return ORM objects from routers).
"""

from __future__ import annotations

import asyncio
import base64
import io
import uuid
import zipfile
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer, get_ws_viewer, require_scope
from app.core.errors import NotFoundError
from app.models.ingest import IngestionRun, RunStatus
from app.schemas.node import NodeCreate, NodeOut
from app.services import node_service as ns
from app.services.ingest.base import IngestItem, KnowledgeIngestor
from app.services.ingest.md_importer import check_zip_limits
from app.workers.tasks.ingest_md import ingest_md

router = APIRouter(prefix="/uploads", tags=["uploads"])

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
# [review-fix 5.R.1] module-level singleton (ruff B008 forbids the factory call
# inline in the Depends default): ingest-item demands the "ingest" scope.
_require_ingest_scope = require_scope("ingest")
_WS_POLL_SECONDS = 0.5
# Application close code for "run invisible OR nonexistent" — one code for
# both, like get_run's generic 404 (a distinct code would confirm the id).
_WS_4404_NOT_FOUND = 4404


class RunOut(BaseModel):
    id: uuid.UUID
    status: RunStatus
    total_items: int
    processed_items: int
    failed_items: int
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class UploadStarted(BaseModel):
    run_id: uuid.UUID
    status: RunStatus


class IngestItemIn(NodeCreate):
    """NodeCreate + `tags` — Confluence labels arrive as tags [plan-fix]."""

    tags: list[str] = Field(default_factory=list)


@router.post(
    "/markdown",
    response_model=UploadStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a zip of Markdown files for ingestion",
    operation_id="uploadMarkdown",
)
async def upload_markdown(
    file: UploadFile,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> UploadStarted:
    # Request-shape validation (not domain logic), so HTTPException is correct here.
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=422, detail="File must be a .zip archive of Markdown files")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 100 MB)")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # [review-fix 5.R.3] zip-bomb caps (declared decompressed size,
            # member count) enforced at the door — same guard parse_zip runs.
            check_zip_limits(zf)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="Not a valid zip file") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run = IngestionRun(
        id=uuid.uuid4(),
        owner_id=viewer.user_id,
        source="md_upload",
        status=RunStatus.pending,
        total_items=0,
    )
    db.add(run)
    await db.commit()  # the worker reads this row — it must be durable before enqueue

    # Primitive args only (kb-celery-jobs rule 2); the zip travels base64.
    try:
        ingest_md.delay(
            str(run.id),
            base64.b64encode(content).decode(),
            str(viewer.user_id),
            viewer.role.value,
            [str(g) for g in viewer.group_ids],
        )
    except Exception as exc:
        # [review-fix 5.R.4] enqueue-then-crash: the run row is already durable
        # but no worker will ever pick it up — a permanent "pending" lie. Mark
        # it failed in a fresh commit and tell the client to retry.
        run.status = RunStatus.failed
        run.error_log = f"enqueue failed: {exc}"
        run.finished_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=503, detail="ingestion queue unavailable, try again later"
        ) from exc

    return UploadStarted(run_id=run.id, status=RunStatus.pending)


@router.post(
    "/ingest-item",
    response_model=NodeOut,
    summary="Upsert a single knowledge node from an external source",
    operation_id="ingestSingleItem",
)
async def ingest_single_item(
    payload: IngestItemIn,
    # [review-fix 5.R.1] service tokens must hold the "ingest" scope; JWT users
    # (scopes=None) pass implicitly. ApiToken.scopes was stored but never read.
    viewer: Viewer = Depends(_require_ingest_scope),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    """Upsert a single knowledge node from an external source (Confluence CLI,
    codebase scanner). Idempotent: same source+source_ref → same node.
    """
    item = IngestItem(
        source=payload.source or "api",
        source_ref=payload.source_ref or str(uuid.uuid4()),
        title=payload.title,
        body=payload.body,
        node_type=payload.node_type,
        visibility=payload.visibility,
        tags=payload.tags,
        meta=payload.meta,
    )
    ingestor = KnowledgeIngestor(db, viewer)
    node = await ingestor.upsert(item)
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)
    return NodeOut.model_validate(node)


@router.get(
    "/runs/{run_id}",
    response_model=RunOut,
    summary="Get ingestion run status",
    operation_id="getIngestionRun",
)
async def get_run(
    run_id: uuid.UUID,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None or run.owner_id != viewer.user_id:
        # Generic body: invisible == nonexistent, nothing confirmed either way.
        raise NotFoundError("Run not found")
    return RunOut.model_validate(run)


@router.websocket("/runs/{run_id}/progress")
async def run_progress_ws(
    run_id: uuid.UUID,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream progress events for an ingestion run (owner only).

    Client receives JSON: {"processed": N, "total": M, "status": "..."}.
    Polls the DB every 500 ms (the worker checkpoints processed_items per item)
    until the run is done or failed.

    [review-fix 5.R.1] The handshake is authenticated (`?token=` or the BFF's
    `access_token` cookie — see get_ws_viewer) and ownership is enforced
    BEFORE any frame is sent: no/bad credentials → 1008 policy violation;
    another user's run or an unknown id → generic 4404 close.

    [review-fix 5.R.2, documented deviation] kb-celery-jobs rule 8 prescribes a
    Redis pub/sub relay (`run:{id}` WsEvent channel); this endpoint polls PG
    instead — correct because the worker checkpoint-commits per item, and one
    less moving part. The pub/sub relay is the Phase 7 hardening upgrade path,
    alongside the MinIO payload offload (4.R.3).
    """
    await websocket.accept()
    try:
        viewer = await get_ws_viewer(websocket, db)
        if viewer is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        while True:
            run = await db.scalar(
                select(IngestionRun)
                .where(IngestionRun.id == run_id)
                # populate_existing: refresh identity-map attributes each poll,
                # or the loop would replay the first read forever [plan-fix].
                .execution_options(populate_existing=True)
            )
            # Ownership re-checked every poll: covers the handshake AND a run
            # deleted mid-stream, with the same generic close either way.
            if run is None or run.owner_id != viewer.user_id:
                await websocket.close(code=_WS_4404_NOT_FOUND)
                return
            await websocket.send_json(
                {
                    "processed": run.processed_items,
                    "total": run.total_items,
                    "status": run.status.value,
                }
            )
            if run.status in (RunStatus.done, RunStatus.failed):
                break
            await asyncio.sleep(_WS_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    await websocket.close()
