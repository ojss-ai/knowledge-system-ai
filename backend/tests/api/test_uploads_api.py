"""Upload API + WebSocket progress tests (Task 5).

[plan-fix] notes vs the Task 5.1 block:
- `ingest_md.delay` is monkeypatched to a recorder (kb-celery-jobs: no live
  broker in unit tests) and the 202 test asserts the enqueued primitive args.
- The plan's `{k: v for k, v in auth_headers.items() if k != "Content-Type"}`
  was a no-op — the fixture only carries Authorization — dropped.
- Added the kb-api-conventions endpoint checklist tests: 401 unauthenticated,
  cross-user read → 404 generic (invisible == nonexistent, get_node standard),
  and a bad-content 422.
- Added WS integration tests for the phase exit criterion ("WebSocket progress
  events confirmed via … integration test"). httpx ASGITransport (the `client`
  fixture) cannot speak WebSocket, so these use starlette's TestClient against
  the real `get_db` — rows are committed to the live PG and cleaned up.
"""

import asyncio
import base64
import concurrent.futures
import io
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.ingest import IngestionRun, RunStatus
from app.models.user import Role, User

# No module-level asyncio pytestmark: asyncio_mode="auto" already collects the
# async tests, and the mark would mis-tag the sync WS integration test.


def make_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def recorded_delay(monkeypatch):
    """Record ingest_md enqueues instead of publishing to the broker."""
    from app.workers.tasks.ingest_md import ingest_md

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(ingest_md, "delay", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


# --- POST /api/v1/uploads/markdown ---


async def test_upload_markdown_returns_202(client: AsyncClient, auth_headers, recorded_delay):
    zip_bytes = make_zip_bytes({"hello.md": "# Hello\n\nContent."})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=auth_headers,
    )
    assert r.status_code == 202
    data = r.json()
    assert "run_id" in data
    assert data["status"] == "pending"

    # The task got the committed run id and primitive viewer args (rule 2).
    assert len(recorded_delay) == 1
    args, kwargs = recorded_delay[0]
    assert kwargs == {}
    run_id, zip_b64, user_id, role, group_ids = args
    assert run_id == data["run_id"]
    assert base64.b64decode(zip_b64) == zip_bytes
    uuid.UUID(user_id)  # a plain str uuid, not an ORM object
    assert role == "user"
    assert group_ids == []


async def test_upload_requires_zip(client: AsyncClient, auth_headers, recorded_delay):
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.txt", io.BytesIO(b"not a zip"), "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert recorded_delay == []


async def test_upload_rejects_invalid_zip_content(
    client: AsyncClient, auth_headers, recorded_delay
):
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(b"zip by name only"), "application/zip")},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert recorded_delay == []


async def test_upload_unauthenticated_is_401(client: AsyncClient):
    zip_bytes = make_zip_bytes({"a.md": "# A"})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
    )
    assert r.status_code == 401


# --- GET /api/v1/uploads/runs/{run_id} ---


async def test_get_run_status(client: AsyncClient, auth_headers):
    zip_bytes = make_zip_bytes({"test.md": "# Test\n\nBody."})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=auth_headers,
    )
    run_id = r.json()["run_id"]
    r2 = await client.get(f"/api/v1/uploads/runs/{run_id}", headers=auth_headers)
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == run_id
    assert body["status"] == "pending"
    assert body["processed_items"] == 0


async def test_get_run_of_another_user_is_404(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Invisible == nonexistent: a 403 would confirm the run id exists."""
    zip_bytes = make_zip_bytes({"secret.md": "# Secret"})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=auth_headers,
    )
    run_id = r.json()["run_id"]
    r2 = await client.get(f"/api/v1/uploads/runs/{run_id}", headers=auth_headers_other)
    assert r2.status_code == 404
    assert r2.json()["detail"] == "Run not found"  # generic body, nothing confirmed


async def test_get_run_missing_is_404(client: AsyncClient, auth_headers):
    r = await client.get(f"/api/v1/uploads/runs/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404


# --- WS /api/v1/uploads/runs/{run_id}/progress (integration, real DB) ---


def _recv_json(ws, timeout: float = 10.0) -> Any:
    """receive_json with a watchdog — a silent server must fail the test, not hang it."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(ws.receive_json).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)


def _run_db(fn: Callable[[AsyncSession], Awaitable[None]]) -> None:
    """Run one committed unit of work on the real DB in a throwaway loop/engine."""

    async def _go() -> None:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as session:
                await fn(session)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_go())


# fastapi re-exports starlette.testclient, which warns about httpx2 at import;
# environmental noise, not ours to fix here.
@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_ws_progress_streams_until_done():
    """Progress events flow over the WS while the run advances, ending at done."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    user_id, run_id = uuid.uuid4(), uuid.uuid4()

    async def _setup(s: AsyncSession) -> None:
        s.add(
            User(
                id=user_id,
                email=f"ws-{uuid.uuid4().hex[:8]}@test.com",
                password_hash="x",
                display_name="ws-test",
                role=Role.user,
            )
        )
        await s.flush()  # no ORM relationship → order the FK parent explicitly
        s.add(
            IngestionRun(
                id=run_id,
                owner_id=user_id,
                source="md_upload",
                status=RunStatus.running,
                total_items=3,
                processed_items=1,
            )
        )

    async def _advance(s: AsyncSession) -> None:
        run = await s.get(IngestionRun, run_id)
        run.processed_items = 3
        run.status = RunStatus.done

    async def _cleanup(s: AsyncSession) -> None:
        await s.execute(delete(IngestionRun).where(IngestionRun.id == run_id))
        await s.execute(delete(User).where(User.id == user_id))

    _run_db(_setup)
    try:
        with TestClient(create_app()) as tc:
            with tc.websocket_connect(f"/api/v1/uploads/runs/{run_id}/progress") as ws:
                first = _recv_json(ws)
                assert first == {"processed": 1, "total": 3, "status": "running"}

                _run_db(_advance)  # the "worker" commits progress behind the WS session

                evt = _recv_json(ws)
                while evt["status"] == "running":
                    evt = _recv_json(ws)
                assert evt == {"processed": 3, "total": 3, "status": "done"}

            # unknown run: one error frame, then the server closes
            with tc.websocket_connect(f"/api/v1/uploads/runs/{uuid.uuid4()}/progress") as ws:
                assert _recv_json(ws) == {"error": "Run not found"}
    finally:
        _run_db(_cleanup)
