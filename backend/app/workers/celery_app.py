from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

celery_app = Celery(
    "kb",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
)

# Auto-discover tasks in app.workers.*
celery_app.autodiscover_tasks(["app.workers"])


@asynccontextmanager
async def task_session() -> AsyncIterator[AsyncSession]:
    """
    Provide a dedicated AsyncSession for use inside Celery tasks.
    Separate from the API session — never share sessions across boundaries.

    Commits on clean exit; on exception the commit is skipped and close()
    rolls the open transaction back. Deliberately NOT wrapped in
    session.begin() ([review-fix 4.R]): long batch tasks commit mid-block
    every N items for resumable progress (kb-celery-jobs rule 5), and a
    single enclosing transaction forbids that (the first inner commit closes
    it and the next statement raises InvalidRequestError).
    """
    from app.core.db import SessionLocal

    async with SessionLocal() as session:
        yield session
        await session.commit()
