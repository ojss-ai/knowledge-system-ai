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
