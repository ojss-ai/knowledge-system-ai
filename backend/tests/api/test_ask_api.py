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
    assert data["degraded"] is False


async def test_ask_requires_auth(client: AsyncClient):
    r = await client.post("/api/v1/ask", json={"query": "test"})
    assert r.status_code == 401


async def test_ask_degrades_on_llm_failure(client: AsyncClient, auth_headers, monkeypatch):
    """2.R.1 (ADR-010): LLM down → 200 with ranked sources, answer null,
    degraded true — and never any raw exception text in the body."""
    import app.api.v1.ask as ask_module

    class _ExplodingLLM:
        async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
            raise RuntimeError("boom-internal-detail")

    monkeypatch.setattr(ask_module, "get_llm", lambda: _ExplodingLLM())

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
    assert data["answer"] is None
    assert data["degraded"] is True
    assert len(data["sources"]) >= 1
    assert "boom-internal-detail" not in r.text
