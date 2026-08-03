from datetime import date

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_create_daily_log(client: AsyncClient, auth_headers):
    today = date.today().isoformat()
    r = await client.post(
        "/api/v1/daily-logs",
        json={"date": today, "body": "Today I worked on the KB system."},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["node_type"] == "daily_log"
    assert data["source"] == "daily_log"


async def test_get_daily_log_by_date(client: AsyncClient, auth_headers):
    today = date.today().isoformat()
    await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "entry"}, headers=auth_headers
    )
    r = await client.get(f"/api/v1/daily-logs/{today}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["source_ref"] == today


async def test_daily_log_idempotent_create(client: AsyncClient, auth_headers):
    """Second POST for same date should upsert, not create duplicate."""
    today = date.today().isoformat()
    r1 = await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "first"}, headers=auth_headers
    )
    r2 = await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "second"}, headers=auth_headers
    )
    assert r1.json()["id"] == r2.json()["id"], "Same date must return same node ID"
    assert r2.json()["body"] == "second"


async def test_daily_log_same_date_different_users(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Uniqueness is per-owner (uq_node_owner_source_ref): user B logging a date
    user A already logged must create B's own node — not 500 on a global
    (source, source_ref) collision."""
    today = date.today().isoformat()
    r1 = await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "user A"}, headers=auth_headers
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "user B"}, headers=auth_headers_other
    )
    assert r2.status_code == 201
    assert r2.json()["id"] != r1.json()["id"]
    assert r2.json()["body"] == "user B"


async def test_daily_log_upsert_recovers_from_insert_race(
    client: AsyncClient, auth_headers, monkeypatch
):
    """SELECT-then-INSERT TOCTOU: if the existence probe misses but the row
    exists (concurrent double-POST), the handler must catch the constraint
    conflict, re-fetch, and update the existing row — upsert semantics, not 500."""
    import app.api.v1.daily_logs as dl

    today = date.today().isoformat()
    r1 = await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "first"}, headers=auth_headers
    )
    assert r1.status_code == 201

    real_probe = dl._existing_log
    calls = {"n": 0}

    async def miss_once(db, viewer, date_str):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # simulate the racer's row landing after our probe
        return await real_probe(db, viewer, date_str)

    monkeypatch.setattr(dl, "_existing_log", miss_once)
    r2 = await client.post(
        "/api/v1/daily-logs", json={"date": today, "body": "second"}, headers=auth_headers
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == r1.json()["id"], "Race loser must converge on the existing node"
    assert r2.json()["body"] == "second"


async def test_daily_log_unauthenticated_401(client: AsyncClient):
    today = date.today().isoformat()
    r = await client.post("/api/v1/daily-logs", json={"date": today, "body": "x"})
    assert r.status_code == 401
    r2 = await client.get(f"/api/v1/daily-logs/{today}")
    assert r2.status_code == 401


async def test_private_daily_log_other_user_looks_not_found(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Mandatory visibility test (kb-visibility-filter): another user's private
    daily log is indistinguishable from a nonexistent one — 404, and neither
    its id nor its body may appear in the response."""
    today = date.today().isoformat()
    r = await client.post(
        "/api/v1/daily-logs",
        json={"date": today, "body": "alpha-secret-journal"},
        headers=auth_headers,
    )
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/daily-logs/{today}", headers=auth_headers_other)
    assert r2.status_code == 404
    assert node_id not in r2.text  # existence
    assert "alpha-secret-journal" not in r2.text  # content
    r3 = await client.get("/api/v1/daily-logs", headers=auth_headers_other)
    assert node_id not in r3.text
    assert "alpha-secret-journal" not in r3.text
