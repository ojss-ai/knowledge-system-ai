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

from httpx import AsyncClient

INGEST_PAYLOAD = {
    "title": "Via service token",
    "body": "synced body",
    "node_type": "confluence_page",
    "visibility": "private",
    "source": "confluence",
    "source_ref": "confluence:TS:svc-auth-1",
}


async def _create_raw_token(client: AsyncClient, auth_headers: dict[str, str]) -> tuple[str, str]:
    r = await client.post(
        "/api/v1/tokens",
        json={"name": "cli", "scopes": ["ingest"]},
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
