# backend/tests/api/test_search_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_search_returns_results(client: AsyncClient, auth_headers):
    # Create a public node with searchable content
    await client.post(
        "/api/v1/nodes",
        json={
            "title": "FastAPI Guide",
            "body": "FastAPI is a modern Python web framework for building APIs.",
            "visibility": "public",
        },
        headers=auth_headers,
    )

    r = await client.get("/api/v1/search?q=FastAPI", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    titles = [item["title"] for item in data["items"]]
    assert any("FastAPI" in t for t in titles)


async def test_search_excludes_private(client: AsyncClient, auth_headers, auth_headers_other):
    await client.post(
        "/api/v1/nodes",
        json={
            "title": "Secret FastAPI Note",
            "body": "top secret fastapi content",
            "visibility": "private",
        },
        headers=auth_headers,
    )

    r = await client.get("/api/v1/search?q=secret+fastapi", headers=auth_headers_other)
    assert r.status_code == 200
    titles = [item["title"] for item in r.json()["items"]]
    assert not any("Secret" in t for t in titles)


async def test_search_requires_auth(client: AsyncClient):
    r = await client.get("/api/v1/search?q=test")
    assert r.status_code == 401
