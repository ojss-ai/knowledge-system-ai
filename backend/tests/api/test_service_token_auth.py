"""Service-token bearer auth (Task 5 gap fix).

The CLI (Task 3/5) sends the raw service token from POST /api/v1/tokens as
`Authorization: Bearer <token>`, but get_current_viewer only decoded JWTs —
so every CLI request would 401. These tests pin the fallback path:

- valid service token → authenticated (resolves to owner, role=service)
- revoked token → 401
- tampered secret / unknown id → 401

Token format is `kb_<token-id-hex>.<secret>` so the ApiToken row is found by
primary key (argon2 hashes are salted — they cannot be looked up by value).
"""

import uuid

from httpx import AsyncClient

INGEST_PAYLOAD = {
    "title": "Via service token",
    "body": "synced body",
    "node_type": "confluence_page",
    "visibility": "private",
    "source": "confluence",
    "source_ref": "confluence:TS:svc-auth-1",
}


async def _create_raw_token(
    client: AsyncClient,
    auth_headers: dict[str, str],
    scopes: list[str] | None = None,
) -> tuple[str, str]:
    r = await client.post(
        "/api/v1/tokens",
        json={"name": "cli", "scopes": scopes if scopes is not None else ["ingest"]},
        headers=auth_headers,
    )
    assert r.status_code == 201
    return r.json()["id"], r.json()["token"]


async def test_service_token_authenticates_ingest_item(client: AsyncClient, auth_headers):
    _, raw = await _create_raw_token(client, auth_headers)
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["source_ref"] == "confluence:TS:svc-auth-1"


async def test_service_token_node_owned_by_token_owner(client: AsyncClient, auth_headers):
    """The service token acts on behalf of its owner: the owner's JWT session
    must see the node the CLI created (owner_id == token owner)."""
    _, raw = await _create_raw_token(client, auth_headers)
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {raw}"},
    )
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{node_id}", headers=auth_headers)
    assert r2.status_code == 200


async def test_revoked_service_token_is_401(client: AsyncClient, auth_headers):
    token_id, raw = await _create_raw_token(client, auth_headers)
    r = await client.delete(f"/api/v1/tokens/{token_id}", headers=auth_headers)
    assert r.status_code == 204
    r2 = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r2.status_code == 401


async def test_tampered_service_token_is_401(client: AsyncClient, auth_headers):
    """Right id prefix, wrong secret — argon2 verify must reject it."""
    _, raw = await _create_raw_token(client, auth_headers)
    prefix, _, _ = raw.rpartition(".")
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {prefix}.wrongsecretwrongsecret"},
    )
    assert r.status_code == 401


async def test_unknown_service_token_id_is_401(client: AsyncClient):
    fake = f"kb_{'0' * 32}.{'a' * 43}"
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {fake}"},
    )
    assert r.status_code == 401


# --- 5.R.1: scope enforcement ------------------------------------------------


async def test_service_token_without_ingest_scope_is_403(client: AsyncClient, auth_headers):
    """A valid token whose scopes lack "ingest" authenticates but is refused."""
    _, raw = await _create_raw_token(client, auth_headers, scopes=["read"])
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 403, r.text


async def test_service_token_with_ingest_scope_is_allowed(client: AsyncClient, auth_headers):
    _, raw = await _create_raw_token(client, auth_headers, scopes=["ingest"])
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code in (200, 201), r.text


async def test_jwt_user_ingests_without_any_scopes(client: AsyncClient, auth_headers):
    """Regression pin: JWT viewers carry scopes=None (all scopes implicitly) —
    scope enforcement must never lock out interactive users."""
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers=auth_headers,
    )
    assert r.status_code in (200, 201), r.text


# --- 5.R.2: timing side-channel ----------------------------------------------


async def test_unknown_token_id_still_pays_argon2_cost(client: AsyncClient, monkeypatch):
    """The not-found branch must run a dummy argon2 verify, so an attacker
    cannot enumerate token ids by timing the 401."""
    from app.core import deps

    class SpyHasher:
        def __init__(self, real):
            self.real = real
            self.verify_calls = 0

        def verify(self, hash_, token):
            self.verify_calls += 1
            return self.real.verify(hash_, token)

    spy = SpyHasher(deps._hasher)
    monkeypatch.setattr(deps, "_hasher", spy)
    fake = f"kb_{'0' * 32}.{'a' * 43}"
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {fake}"},
    )
    assert r.status_code == 401
    assert spy.verify_calls == 1, "not-found branch skipped the dummy argon2 verify"


# --- 5.R.3: corrupt stored hash is a bad credential, not a server error -------


async def test_corrupt_token_hash_is_401_not_500(client: AsyncClient, auth_headers, db):
    """argon2 raises InvalidHashError (not VerificationError) on a malformed
    stored hash — it must map to 401, never a 500."""
    from app.models.ingest import ApiToken

    token_id, raw = await _create_raw_token(client, auth_headers)
    row = await db.get(ApiToken, uuid.UUID(token_id))
    row.token_hash = "not-an-argon2-hash"
    await db.flush()
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json=INGEST_PAYLOAD,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 401
