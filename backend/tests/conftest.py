import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import get_db
from app.main import create_app

TEST_DB_URL = settings.database_url  # same dockerized PG; tests roll back


@pytest.fixture
async def db():
    engine = create_async_engine(TEST_DB_URL)
    conn = await engine.connect()
    txn = await conn.begin()
    session = async_sessionmaker(bind=conn, expire_on_commit=False, class_=AsyncSession)()
    yield session
    await session.close()
    await txn.rollback()
    await conn.close()
    await engine.dispose()


@pytest.fixture
async def client(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
