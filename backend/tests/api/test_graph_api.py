"""API tests for /api/v1/edges and /api/v1/graph (Task 9).

Tests that create or traverse edges need a live Neo4j, so they take the
``neo4j_session`` fixture and skip when Neo4j is unreachable. The visibility
tests are pure-PG (the invisible-node 404 fires before any Neo4j call, and an
empty overview never reaches Neo4j) and always run.
"""

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_node(
    client: AsyncClient, headers: dict[str, str], title: str, visibility: str = "public"
) -> str:
    r = await client.post(
        "/api/v1/nodes", json={"title": title, "visibility": visibility}, headers=headers
    )
    assert r.status_code == 201
    return r.json()["id"]


async def test_create_edge(client: AsyncClient, auth_headers, neo4j_session):
    n1_id = await _create_node(client, auth_headers, "N1")
    n2_id = await _create_node(client, auth_headers, "N2")
    r = await client.post(
        "/api/v1/edges",
        json={"source_id": n1_id, "target_id": n2_id, "label": "LINKS_TO"},
        headers=auth_headers,
    )
    assert r.status_code == 201


async def test_delete_edge(client: AsyncClient, auth_headers, neo4j_session):
    n1_id = await _create_node(client, auth_headers, "D1")
    n2_id = await _create_node(client, auth_headers, "D2")
    payload = {"source_id": n1_id, "target_id": n2_id, "label": "LINKS_TO"}
    await client.post("/api/v1/edges", json=payload, headers=auth_headers)
    r = await client.request("DELETE", "/api/v1/edges", json=payload, headers=auth_headers)
    assert r.status_code == 204
    hood = await client.get(f"/api/v1/graph/neighborhood/{n1_id}?hops=1", headers=auth_headers)
    assert n2_id not in [e["target"] for e in hood.json()["edges"]]


async def test_neighborhood(client: AsyncClient, auth_headers, neo4j_session):
    n1_id = await _create_node(client, auth_headers, "Center")
    n2_id = await _create_node(client, auth_headers, "Neighbour")
    await client.post(
        "/api/v1/edges",
        json={"source_id": n1_id, "target_id": n2_id, "label": "LINKS_TO"},
        headers=auth_headers,
    )
    r = await client.get(f"/api/v1/graph/neighborhood/{n1_id}?hops=1", headers=auth_headers)
    assert r.status_code == 200
    ids = [n["id"] for n in r.json()["nodes"]]
    assert n2_id in ids


async def test_graph_overview(client: AsyncClient, auth_headers):
    r = await client.get("/api/v1/graph/overview", headers=auth_headers)
    assert r.status_code == 200
    assert "nodes" in r.json()
    assert "edges" in r.json()


async def test_create_edge_unauthenticated_401(client: AsyncClient):
    r = await client.post(
        "/api/v1/edges",
        json={"source_id": str(uuid.uuid4()), "target_id": str(uuid.uuid4())},
    )
    assert r.status_code == 401


async def test_create_edge_unknown_label_422(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/edges",
        json={
            "source_id": str(uuid.uuid4()),
            "target_id": str(uuid.uuid4()),
            "label": "TOTALLY_MADE_UP",
        },
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_neighborhood_hops_above_limit_422(client: AsyncClient, auth_headers):
    nid = await _create_node(client, auth_headers, "HopNode")
    r = await client.get(f"/api/v1/graph/neighborhood/{nid}?hops=9", headers=auth_headers)
    assert r.status_code == 422


async def test_create_edge_to_invisible_target_looks_not_found(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Attaching an edge to a node the caller cannot see must 404 without
    confirming existence (ADR-004) — checked in PG before any Neo4j write."""
    secret_id = await _create_node(client, auth_headers, "edge-secret", visibility="private")
    mine_id = await _create_node(client, auth_headers_other, "Mine")
    r = await client.post(
        "/api/v1/edges",
        json={"source_id": mine_id, "target_id": secret_id, "label": "LINKS_TO"},
        headers=auth_headers_other,
    )
    assert r.status_code == 404
    assert secret_id not in r.text  # existence
    assert "edge-secret" not in r.text  # content


async def test_delete_edge_invisible_target_looks_not_found(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """DELETE must vet BOTH endpoints exactly like create: a target the caller
    cannot see must 404 before any Neo4j call (ADR-004) — otherwise a caller
    could probe (or detach) edges into another user's private nodes."""
    secret_id = await _create_node(client, auth_headers, "del-secret", visibility="private")
    mine_id = await _create_node(client, auth_headers_other, "DelMine")
    r = await client.request(
        "DELETE",
        "/api/v1/edges",
        json={"source_id": mine_id, "target_id": secret_id, "label": "LINKS_TO"},
        headers=auth_headers_other,
    )
    assert r.status_code == 404
    assert secret_id not in r.text  # existence
    assert "del-secret" not in r.text  # content


async def test_neighborhood_invisible_center_looks_not_found(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Neighborhood of an invisible center is indistinguishable from a
    nonexistent one: 404, generic body (kb-visibility-filter)."""
    secret_id = await _create_node(client, auth_headers, "hood-secret", visibility="private")
    r = await client.get(f"/api/v1/graph/neighborhood/{secret_id}", headers=auth_headers_other)
    assert r.status_code == 404
    assert secret_id not in r.text  # existence
    assert "hood-secret" not in r.text  # content


async def test_overview_hides_other_users_private(
    client: AsyncClient, auth_headers, auth_headers_other
):
    secret_id = await _create_node(client, auth_headers, "overview-secret", visibility="private")
    r = await client.get("/api/v1/graph/overview", headers=auth_headers_other)
    assert r.status_code == 200
    assert secret_id not in r.text  # existence
    assert "overview-secret" not in r.text  # content
