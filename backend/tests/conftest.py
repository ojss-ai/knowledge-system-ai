import socket
import uuid as _uuid
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.db import get_db
from app.core.neo4j import ensure_constraints, get_driver
from app.main import create_app
from app.models.user import Role, User, Visibility

TEST_DB_URL = settings.database_url  # same dockerized PG; tests roll back


def _neo4j_available() -> bool:
    """Fast TCP probe so Neo4j tests skip (not hang/error) when the service is down."""
    parsed = urlparse(settings.neo4j_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 7687
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture
async def neo4j_session():
    if not _neo4j_available():
        pytest.skip("Neo4j unreachable")
    await ensure_constraints()  # tests must not depend on app lifespan having run
    async with get_driver().session() as session:
        yield session
        # Teardown: wipe all test nodes
        await session.run("MATCH (n:Node) WHERE n.test = true DETACH DELETE n")


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


async def _register_and_login(db, client, email: str, *, role: Role = Role.user) -> dict[str, str]:
    """Register via auth_service (there is no /auth/register endpoint) and log in.

    [plan-fix, Task 8.5]: the plan's fixture used POST /api/v1/auth/register and
    form-data login; the real API has JSON login only and no register endpoint.
    """
    from app.services import auth_service

    await auth_service.register(
        db, email=email, password="pass1234", display_name=email.split("@")[0], role=role
    )
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "pass1234"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def auth_headers(db, client):
    return await _register_and_login(db, client, "owner@test.com")


@pytest_asyncio.fixture
async def auth_headers_other(db, client):
    return await _register_and_login(db, client, "other@test.com")


@pytest_asyncio.fixture
async def auth_headers_admin(db, client):
    return await _register_and_login(db, client, "admin@test.com", role=Role.admin)


@pytest_asyncio.fixture
async def make_user(db):
    """Factory: create a User in the test DB and return it."""

    async def _factory(
        *,
        email: str,
        display_name: str = "Test User",
        role: Role = Role.user,
        password_hash: str = "x",
    ) -> User:
        user = User(
            id=_uuid.uuid4(),
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            role=role,
        )
        db.add(user)
        await db.flush()
        return user

    return _factory


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
