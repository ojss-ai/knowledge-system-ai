"""Service token API tests (Task 6).

[plan-fix] notes vs the Task 6.1 block:
- Dropped `pytestmark = pytest.mark.asyncio` — asyncio_mode="auto" already
  collects async tests (test_uploads_api precedent).
- Added the kb-api-conventions endpoint checklist tests: 401 unauthenticated,
  cross-user revoke → 404 generic (invisible == nonexistent, get_run
  standard), and a 422 validation case.
- Added hashed-at-rest evidence: the DB row must hold an argon2 hash that
  verifies against the raw token, never the plaintext; list/revoke responses
  must never carry the raw token or the hash.
"""

import uuid

from argon2 import PasswordHasher
from httpx import AsyncClient
from sqlalchemy import select

from app.models.ingest import ApiToken

# --- POST /api/v1/tokens ---


async def test_create_token(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/tokens",
        json={"name": "confluence-sync", "scopes": ["ingest", "read"]},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert "token" in data  # raw token returned once
    assert "id" in data
    assert data["name"] == "confluence-sync"
    assert data["scopes"] == ["ingest", "read"]


async def test_create_token_stores_hash_not_plaintext(client: AsyncClient, auth_headers, db):
    r = await client.post(
        "/api/v1/tokens", json={"name": "hashed", "scopes": ["read"]}, headers=auth_headers
    )
    raw = r.json()["token"]
    row = await db.scalar(select(ApiToken).where(ApiToken.id == uuid.UUID(r.json()["id"])))
    assert row.token_hash != raw
    assert raw not in row.token_hash
    PasswordHasher().verify(row.token_hash, raw)  # raises on mismatch


async def test_create_token_missing_name_is_422(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/tokens", json={"scopes": ["read"]}, headers=auth_headers)
    assert r.status_code == 422


async def test_create_token_unauthenticated_is_401(client: AsyncClient):
    r = await client.post("/api/v1/tokens", json={"name": "t", "scopes": ["read"]})
    assert r.status_code == 401


# --- GET /api/v1/tokens ---


async def test_list_tokens(client: AsyncClient, auth_headers):
    await client.post(
        "/api/v1/tokens", json={"name": "t1", "scopes": ["read"]}, headers=auth_headers
    )
    r = await client.get("/api/v1/tokens", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body) >= 1
    for item in body:
        assert "token" not in item  # raw token is shown once, at creation only
        assert "token_hash" not in item


async def test_list_tokens_excludes_other_users(
    client: AsyncClient, auth_headers, auth_headers_other
):
    await client.post(
        "/api/v1/tokens", json={"name": "mine", "scopes": ["read"]}, headers=auth_headers
    )
    r = await client.get("/api/v1/tokens", headers=auth_headers_other)
    assert r.status_code == 200
    assert all(item["name"] != "mine" for item in r.json())


async def test_list_tokens_unauthenticated_is_401(client: AsyncClient):
    r = await client.get("/api/v1/tokens")
    assert r.status_code == 401


# --- DELETE /api/v1/tokens/{token_id} ---


async def test_revoke_token(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/tokens", json={"name": "t2", "scopes": ["read"]}, headers=auth_headers
    )
    tid = r.json()["id"]
    r2 = await client.delete(f"/api/v1/tokens/{tid}", headers=auth_headers)
    assert r2.status_code == 204

    # revoked tokens drop out of the list
    r3 = await client.get("/api/v1/tokens", headers=auth_headers)
    assert all(item["id"] != tid for item in r3.json())


async def test_revoke_other_users_token_is_404(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Invisible == nonexistent: a 403 would confirm the token id exists."""
    r = await client.post(
        "/api/v1/tokens", json={"name": "victim", "scopes": ["read"]}, headers=auth_headers
    )
    tid = r.json()["id"]
    r2 = await client.delete(f"/api/v1/tokens/{tid}", headers=auth_headers_other)
    assert r2.status_code == 404
    assert r2.json()["detail"] == "Token not found"  # generic body, nothing confirmed


async def test_revoke_missing_token_is_404(client: AsyncClient, auth_headers):
    r = await client.delete(f"/api/v1/tokens/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404
