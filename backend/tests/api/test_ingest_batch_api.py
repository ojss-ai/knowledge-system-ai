"""ingest-batch API tests (Task 4a). Auth/idempotency mirror test_ingest_item_api."""

from httpx import AsyncClient


def _item(ref: str, title: str, body: str = "b") -> dict:
    return {
        "title": title,
        "body": body,
        "node_type": "code_symbol",
        "source": "codebase",
        "source_ref": ref,
        "tags": ["code"],
    }


async def test_batch_creates_nodes_and_queues_edges(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/f.py", "f.py"), _item("r/f.py#f.alpha", "alpha")],
            "edges": [{"source_ref": "r/f.py", "target_ref": "r/f.py#f.alpha", "label": "DEFINES"}],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["created"] == 2 and data["edges_queued"] == 1 and data["edges_dangling"] == 0


async def test_batch_idempotent_second_run_creates_nothing(client: AsyncClient, auth_headers):
    payload = {"items": [_item("r/idem.py", "idem.py")], "edges": []}
    await client.post("/api/v1/uploads/ingest-batch", json=payload, headers=auth_headers)
    r2 = await client.post("/api/v1/uploads/ingest-batch", json=payload, headers=auth_headers)
    assert r2.json()["created"] == 0 and r2.json()["skipped"] == 1


async def test_batch_resolves_ref_from_previous_request(client: AsyncClient, auth_headers):
    await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={"items": [_item("r/prev.py#p.f", "f")], "edges": []},
    )
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/next.py#n.g", "g")],
            "edges": [
                {
                    "source_ref": "r/next.py#n.g",
                    "target_ref": "r/prev.py#p.f",
                    "label": "CALLS",
                    "confidence": 0.7,
                }
            ],
        },
    )
    assert r.json()["edges_queued"] == 1 and r.json()["edges_dangling"] == 0


async def test_batch_fallback_never_crosses_sources(client: AsyncClient, auth_headers):
    """[4.R.1] Same owner, same source_ref under another source: dangling, never mislinked."""
    await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [{"title": "doc", "body": "b", "source": "md_upload", "source_ref": "X"}],
            "edges": [],
        },
    )
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/c.py#c.f", "f")],
            "edges": [{"source_ref": "r/c.py#c.f", "target_ref": "X", "label": "CALLS"}],
        },
    )
    assert r.status_code == 200
    assert r.json()["edges_queued"] == 0 and r.json()["edges_dangling"] == 1


async def test_batch_edge_only_with_fallback_source_resolves(client: AsyncClient, auth_headers):
    """[4.R.1] Scanner edge batches carry no items; explicit fallback_source scopes them."""
    await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={"items": [_item("r/eo1.py#a", "a"), _item("r/eo2.py#b", "b")], "edges": []},
    )
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [],
            "edges": [
                {
                    "source_ref": "r/eo1.py#a",
                    "target_ref": "r/eo2.py#b",
                    "label": "CALLS",
                    "confidence": 0.7,
                }
            ],
            "fallback_source": "codebase",
        },
    )
    assert r.json()["edges_queued"] == 1 and r.json()["edges_dangling"] == 0


async def test_batch_edge_only_without_fallback_source_is_dangling(
    client: AsyncClient, auth_headers
):
    """[4.R.1] No items and no fallback_source: the DB probe is skipped (never unscoped)."""
    await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={"items": [_item("r/eo3.py#a", "a"), _item("r/eo4.py#b", "b")], "edges": []},
    )
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [],
            "edges": [{"source_ref": "r/eo3.py#a", "target_ref": "r/eo4.py#b", "label": "CALLS"}],
        },
    )
    assert r.json()["edges_queued"] == 0 and r.json()["edges_dangling"] == 1


async def test_batch_counts_dangling_edges(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [_item("r/only.py", "only.py")],
            "edges": [{"source_ref": "r/only.py", "target_ref": "r/ghost.py", "label": "DEFINES"}],
        },
    )
    assert r.status_code == 200 and r.json()["edges_dangling"] == 1


async def test_batch_unknown_label_is_422(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-batch",
        headers=auth_headers,
        json={
            "items": [],
            "edges": [{"source_ref": "a", "target_ref": "b", "label": "DEFINED_IN"}],
        },
    )
    assert r.status_code == 422


async def test_batch_unauthenticated_is_401(client: AsyncClient):
    r = await client.post("/api/v1/uploads/ingest-batch", json={"items": [], "edges": []})
    assert r.status_code == 401
