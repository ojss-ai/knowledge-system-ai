import pytest
from sqlalchemy import text

from app.workers.celery_app import celery_app, task_session


def test_celery_app_name():
    assert celery_app.main == "kb"


def test_celery_config():
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_serializer == "json"
    assert conf.result_serializer == "json"


def test_task_session_is_context_manager():
    """task_session() must produce an async context manager."""
    cm = task_session()
    assert hasattr(cm, "__aenter__")
    assert hasattr(cm, "__aexit__")


@pytest.mark.asyncio
async def test_task_session_supports_mid_block_batch_commit():
    """[review-fix 4.R] long batch tasks (ingest) commit every N items INSIDE
    one task_session for resumable progress (kb-celery-jobs rule 5). Wrapping
    the whole block in session.begin() forbids that: the first mid-block commit
    closes the enclosing transaction and the very next statement raises
    InvalidRequestError ("Can't operate on closed transaction")."""
    async with task_session() as db:
        await db.commit()  # batch durability boundary
        assert await db.scalar(text("SELECT 1")) == 1  # next batch still works
