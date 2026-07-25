import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_ask_returns_answer(client: AsyncClient, auth_headers):
    # Create a node with content
    await client.post(
        "/api/v1/nodes",
        json={
            "title": "FastAPI",
            "body": "FastAPI is a modern Python web framework.",
            "visibility": "public",
        },
        headers=auth_headers,
    )

    r = await client.post(
        "/api/v1/ask",
        json={"query": "What is FastAPI?"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data
    assert "sources" in data
    assert isinstance(data["sources"], list)


async def test_ask_requires_auth(client: AsyncClient):
    r = await client.post("/api/v1/ask", json={"query": "test"})
    assert r.status_code == 401
