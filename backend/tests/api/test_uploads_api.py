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
- [review-fix 5.R.1] The WS handshake now authenticates (`?token=` query param
  or the BFF's `access_token` cookie) and enforces run ownership: bad/missing
  token → 1008 policy violation; another user's or an unknown run → generic
  4404 close with no progress frame leaked.
"""

import asyncio
import base64
import concurrent.futures
import contextlib
import io
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from fastapi import WebSocketDisconnect
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import make_access_token
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


def _expect_ws_close(ws, expected_code: int) -> None:
    """The server must close (with expected_code) WITHOUT sending any frame —
    a data frame before the close means progress leaked and fails here too."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        _recv_json(ws)
    assert exc_info.value.code == expected_code


def _ws_app():
    """create_app for WS tests, with get_db overridden to a NullPool engine.

    The real get_db uses the module-global engine: its pool would hand a
    second WS test an asyncpg connection bound to the FIRST test's (dead)
    event loop — TestClient spins a fresh loop per instance → "attached to a
    different loop". NullPool opens/closes per request inside the live loop.
    """
    from app.core.db import get_db
    from app.main import create_app

    app = create_app()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)

    async def _get_db():
        async with AsyncSession(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    return app


@contextlib.contextmanager
def _ws_connect(tc, url: str, **kwargs):
    """websocket_connect whose teardown tolerates a SERVER-initiated close:
    starlette's WebSocketTestSession.__exit__ races the server task and can
    raise ClosedResourceError sending its own disconnect — benign here."""
    cm = tc.websocket_connect(url, **kwargs)
    ws = cm.__enter__()
    try:
        yield ws
    finally:
        with contextlib.suppress(anyio.ClosedResourceError):
            cm.__exit__(None, None, None)


# fastapi re-exports starlette.testclient, which warns about httpx2 at import;
# environmental noise, not ours to fix here.
@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_ws_progress_streams_until_done():
    """Progress events flow over the WS while the run advances, ending at done.

    [review-fix 5.R.1] The owner authenticates the handshake — via `?token=`
    for the streaming connection and via the `access_token` cookie (the BFF
    same-origin path, ADR-008) for the re-check; an unknown run id now yields
    a generic 4404 close instead of an error frame."""
    from fastapi.testclient import TestClient

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
    token = make_access_token(user_id, "user")
    url = f"/api/v1/uploads/runs/{run_id}/progress"
    try:
        with TestClient(_ws_app()) as tc:
            with _ws_connect(tc, f"{url}?token={token}") as ws:
                first = _recv_json(ws)
                assert first == {"processed": 1, "total": 3, "status": "running"}

                _run_db(_advance)  # the "worker" commits progress behind the WS session

                evt = _recv_json(ws)
                while evt["status"] == "running":
                    evt = _recv_json(ws)
                assert evt == {"processed": 3, "total": 3, "status": "done"}

            # cookie-auth handshake (browser through the Next.js BFF: cookies
            # flow on the same-origin WS upgrade): run is done → final frame
            with _ws_connect(tc, url, headers={"cookie": f"access_token={token}"}) as ws:
                assert _recv_json(ws)["status"] == "done"

            # unknown run (authenticated): generic 4404 close, no frame —
            # indistinguishable from someone else's run
            with _ws_connect(
                tc, f"/api/v1/uploads/runs/{uuid.uuid4()}/progress?token={token}"
            ) as ws:
                _expect_ws_close(ws, 4404)
    finally:
        _run_db(_cleanup)


@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_ws_progress_auth_required_and_ownership_enforced():
    """[review-fix 5.R.1] The WS handshake authenticates and enforces
    ownership: missing/bad token → 1008 policy violation; a valid token for
    ANOTHER user → generic 4404 (invisible == nonexistent) with no progress
    frame leaked before the close."""
    from fastapi.testclient import TestClient

    owner_id, intruder_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _setup(s: AsyncSession) -> None:
        s.add(
            User(
                id=owner_id,
                email=f"wsa-{uuid.uuid4().hex[:8]}@test.com",
                password_hash="x",
                display_name="ws-owner",
                role=Role.user,
            )
        )
        s.add(
            User(
                id=intruder_id,
                email=f"wsb-{uuid.uuid4().hex[:8]}@test.com",
                password_hash="x",
                display_name="ws-intruder",
                role=Role.user,
            )
        )
        await s.flush()
        s.add(
            IngestionRun(
                id=run_id,
                owner_id=owner_id,
                source="md_upload",
                status=RunStatus.running,
                total_items=3,
                processed_items=1,
            )
        )

    async def _cleanup(s: AsyncSession) -> None:
        await s.execute(delete(IngestionRun).where(IngestionRun.id == run_id))
        await s.execute(delete(User).where(User.id.in_([owner_id, intruder_id])))

    url = f"/api/v1/uploads/runs/{run_id}/progress"
    _run_db(_setup)
    try:
        with TestClient(_ws_app()) as tc:
            # no credentials at all
            with _ws_connect(tc, url) as ws:
                _expect_ws_close(ws, 1008)

            # garbage token
            with _ws_connect(tc, f"{url}?token=not-a-jwt") as ws:
                _expect_ws_close(ws, 1008)

            # valid token, but NOT the run's owner: denied exactly like a
            # missing run — a distinct code would confirm the run id exists
            intruder_token = make_access_token(intruder_id, "user")
            with _ws_connect(tc, f"{url}?token={intruder_token}") as ws:
                _expect_ws_close(ws, 4404)
    finally:
        _run_db(_cleanup)
