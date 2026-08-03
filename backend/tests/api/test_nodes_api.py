import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_create_node(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "My Note", "body": "hello world"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "My Note"
    assert "id" in data
    assert "body_tsv" not in data
    assert "password_hash" not in data


async def test_create_node_unauthenticated_401(client: AsyncClient):
    r = await client.post("/api/v1/nodes", json={"title": "Nope"})
    assert r.status_code == 401


async def test_create_node_missing_title_422(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"body": "no title"}, headers=auth_headers)
    assert r.status_code == 422


async def test_get_node_own(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"title": "GetMe"}, headers=auth_headers)
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{node_id}", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == node_id


async def test_get_private_node_other_user_looks_not_found(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Invisible nodes must be indistinguishable from nonexistent ones (ADR-004):
    404, and neither the id nor the title may appear in the response body."""
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "PrivateSecretTitle", "visibility": "private"},
        headers=auth_headers,
    )
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{node_id}", headers=auth_headers_other)
    assert r2.status_code == 404
    assert node_id not in r2.text  # existence
    assert "PrivateSecretTitle" not in r2.text  # content


async def test_list_nodes(client: AsyncClient, auth_headers):
    for i in range(3):
        await client.post("/api/v1/nodes", json={"title": f"Node {i}"}, headers=auth_headers)
    r = await client.get("/api/v1/nodes", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert data["total"] >= 3


async def test_list_nodes_hides_other_users_private(
    client: AsyncClient, auth_headers, auth_headers_other
):
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "alpha-secret", "visibility": "private"},
        headers=auth_headers,
    )
    node_id = r.json()["id"]
    r2 = await client.get("/api/v1/nodes", headers=auth_headers_other)
    assert r2.status_code == 200
    assert "alpha-secret" not in r2.text  # content
    assert node_id not in r2.text  # existence


async def test_update_node(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"title": "Old"}, headers=auth_headers)
    nid = r.json()["id"]
    r2 = await client.patch(f"/api/v1/nodes/{nid}", json={"title": "New"}, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["title"] == "New"


async def test_delete_node(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"title": "ToDelete"}, headers=auth_headers)
    nid = r.json()["id"]
    r2 = await client.delete(f"/api/v1/nodes/{nid}", headers=auth_headers)
    assert r2.status_code == 204
    r3 = await client.get(f"/api/v1/nodes/{nid}", headers=auth_headers)
    assert r3.status_code == 404


async def test_admin_gets_no_visibility_bypass_outside_admin_routes(
    client: AsyncClient, auth_headers, auth_headers_admin
):
    """Review carry-over from Task 4: the Viewer(role=admin) bypass in
    visible_nodes_clause must not be reachable outside /api/v1/admin/*
    (kb-visibility-filter rule 5)."""
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "owner-only-secret", "visibility": "private"},
        headers=auth_headers,
    )
    nid = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{nid}", headers=auth_headers_admin)
    assert r2.status_code == 404  # invisible == nonexistent, even for admin outside /admin
    assert "owner-only-secret" not in r2.text
    r3 = await client.get("/api/v1/nodes", headers=auth_headers_admin)
    assert "owner-only-secret" not in r3.text
    assert nid not in r3.text


async def test_share_grants_visibility(client: AsyncClient, auth_headers, auth_headers_other):
    me = await client.get("/api/v1/users/me", headers=auth_headers_other)
    other_id = me.json()["id"]
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "Team Doc", "visibility": "shared"},
        headers=auth_headers,
    )
    nid = r.json()["id"]
    # invisible before the share row exists — looks nonexistent (ADR-004)
    pre = await client.get(f"/api/v1/nodes/{nid}", headers=auth_headers_other)
    assert pre.status_code == 404
    assert nid not in pre.text
    rs = await client.post(
        f"/api/v1/nodes/{nid}/shares", json={"user_id": other_id}, headers=auth_headers
    )
    assert rs.status_code == 201
    post = await client.get(f"/api/v1/nodes/{nid}", headers=auth_headers_other)
    assert post.status_code == 200


async def test_share_by_non_owner_forbidden(client: AsyncClient, auth_headers, auth_headers_other):
    """A viewer must not be able to extend visibility of a node they do not own."""
    me = await client.get("/api/v1/users/me", headers=auth_headers_other)
    other_id = me.json()["id"]
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "Public Doc", "visibility": "public"},
        headers=auth_headers,
    )
    nid = r.json()["id"]
    rs = await client.post(
        f"/api/v1/nodes/{nid}/shares", json={"user_id": other_id}, headers=auth_headers_other
    )
    assert rs.status_code == 403
