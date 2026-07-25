"""Ingest-item API tests (Task 4).

[plan-fix] notes vs the Task 4.1 block:
- Dropped `pytestmark = pytest.mark.asyncio` — asyncio_mode="auto" already
  collects async tests (test_tokens_api precedent).
- Added the kb-api-conventions endpoint checklist tests: 401 unauthenticated
  and a 422 validation case.
- Added tags-persistence test: the sync engine (Task 3) sends Confluence
  labels as `tags` and IngestItem supports them, but the plan's endpoint block
  dropped them silently (NodeCreate has no tags field) — that would break the
  labels → tags rule (kb-ingestion-connectors).
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select


async def test_ingest_item_creates_node(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json={
            "title": "Confluence Page",
            "body": "# Title\n\nContent from Confluence.",
            "node_type": "confluence_page",
            "visibility": "private",
            "source": "confluence",
            "source_ref": "confluence:TS:12345",
            "meta": {"confluence_page_id": "12345"},
            "tags": ["confluence", "docs"],
        },
        headers=auth_headers,
    )
    assert r.status_code in (200, 201)
    data = r.json()
    assert data["source"] == "confluence"
    assert data["source_ref"] == "confluence:TS:12345"


async def test_ingest_item_idempotent(client: AsyncClient, auth_headers):
    payload = {
        "title": "Idem Page",
        "body": "body",
        "source": "confluence",
        "source_ref": "confluence:TS:idem1",
    }
    r1 = await client.post("/api/v1/uploads/ingest-item", json=payload, headers=auth_headers)
    r2 = await client.post("/api/v1/uploads/ingest-item", json=payload, headers=auth_headers)
    assert r1.json()["id"] == r2.json()["id"], "Same source_ref must return same node ID"


async def test_ingest_item_persists_tags(client: AsyncClient, auth_headers, db):
    """Confluence labels arrive as `tags` and must land in tags/node_tags."""
    from app.models.knowledge import NodeTag, Tag

    r = await client.post(
        "/api/v1/uploads/ingest-item",
        json={
            "title": "Tagged Page",
            "body": "body",
            "source": "confluence",
            "source_ref": "confluence:TS:tagged1",
            "tags": ["confluence", "docs"],
        },
        headers=auth_headers,
    )
    node_id = uuid.UUID(r.json()["id"])
    slugs = await db.scalars(
        select(Tag.slug).join(NodeTag, NodeTag.tag_id == Tag.id).where(NodeTag.node_id == node_id)
    )
    assert set(slugs) == {"confluence", "docs"}


async def test_ingest_item_unauthenticated_is_401(client: AsyncClient):
    r = await client.post("/api/v1/uploads/ingest-item", json={"title": "t", "body": "b"})
    assert r.status_code == 401


async def test_ingest_item_missing_title_is_422(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/ingest-item", json={"body": "no title"}, headers=auth_headers
    )
    assert r.status_code == 422
