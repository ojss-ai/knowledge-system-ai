import uuid

import pytest
from sqlalchemy import select

from app.models.ingest import ApiToken, IngestionRun, RunStatus

pytestmark = pytest.mark.asyncio


async def test_ingestion_run_lifecycle(db, make_user):
    owner = await make_user(email="ingest@test.com")
    run = IngestionRun(
        id=uuid.uuid4(),
        owner_id=owner.id,
        source="md_upload",
        status=RunStatus.pending,
        total_items=5,
    )
    db.add(run)
    await db.flush()

    result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert result.status == RunStatus.pending
    assert result.total_items == 5

    result.status = RunStatus.done
    result.processed_items = 5
    await db.flush()
    updated = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert updated.status == RunStatus.done


async def test_api_token_roundtrip(db, make_user):
    # [plan-fix] plan shipped ApiToken with no test; TDD iron law requires one.
    owner = await make_user(email="token@test.com")
    token = ApiToken(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="confluence-sync",
        token_hash="sha256$abc123",
        scopes=["ingest", "read"],
    )
    db.add(token)
    await db.flush()

    result = await db.scalar(select(ApiToken).where(ApiToken.id == token.id))
    assert result.scopes == ["ingest", "read"]
    assert result.revoked is False
    assert result.expires_at is None
