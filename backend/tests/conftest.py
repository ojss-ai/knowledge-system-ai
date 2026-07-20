import uuid as _uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import get_db
from app.main import create_app
from app.models.user import Visibility

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


@pytest_asyncio.fixture
async def make_node(db):
    """Factory: create a KnowledgeNode in the test DB and return it."""
    # Imported lazily: app.models.knowledge lands in Task 2; a top-level import
    # would break collection of the whole suite until then.
    from app.models.knowledge import KnowledgeNode

    created: list[KnowledgeNode] = []

    async def _factory(
        owner,
        *,
        title: str = "Test Node",
        body: str = "body text",
        visibility: Visibility = Visibility.private,
        node_type: str = "note",
        source: str | None = None,
        source_ref: str | None = None,
    ) -> KnowledgeNode:
        node = KnowledgeNode(
            id=_uuid.uuid4(),
            owner_id=owner.id,
            title=title,
            body=body,
            visibility=visibility,
            node_type=node_type,
            source=source,
            source_ref=source_ref,
        )
        db.add(node)
        await db.flush()
        created.append(node)
        return node

    yield _factory
    # cleanup handled by transactional rollback


@pytest_asyncio.fixture
async def make_tag(db):
    """Factory: create a Tag."""
    from app.models.knowledge import Tag  # lazy: model lands in Task 2

    async def _factory(name: str = "test-tag") -> Tag:
        tag = Tag(id=_uuid.uuid4(), name=name, slug=name.lower().replace(" ", "-"))
        db.add(tag)
        await db.flush()
        return tag

    return _factory
