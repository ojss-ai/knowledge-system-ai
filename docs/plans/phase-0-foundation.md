# Phase 0: Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `kb-executing-plans`. Steps use checkbox (`- [ ]`) syntax for tracking. One task at a time.

**Goal:** Running dev environment: Postgres(+pgvector)/Neo4j/Redis/MinIO in Docker, FastAPI skeleton with JWT auth, users & groups managed by an admin, full test infrastructure.

**Architecture:** Modular monolith (ADR-005); auth per ADR-008; single Postgres per ADR-001.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, argon2-cffi, PyJWT, structlog, pytest + httpx, Docker Compose.

**Required skills:** `kb-conventions`, `kb-tdd-workflow`, `kb-api-conventions`.

**Exit criteria:** `/kb-verify` green; `docker compose up` from clean checkout → login as seeded admin via curl works; admin can create a user and a group via API; visibility of this phase = n/a (no knowledge nodes yet).

**Branch:** `phase-0-foundation`

---

### Task 1: Repo scaffold & tooling

**Files:**
- Create: `.gitignore`, `Makefile`, `backend/pyproject.toml`, `backend/app/__init__.py`, `backend/tests/__init__.py`

- [ ] **Step 1: Create branch and structure**

```bash
git checkout -b phase-0-foundation
mkdir -p backend/app backend/tests frontend tools docker
touch backend/app/__init__.py backend/tests/__init__.py
```

- [ ] **Step 2: Write `backend/pyproject.toml`**

```toml
[project]
name = "kb-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.111",
  "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0.30",
  "asyncpg>=0.29",
  "alembic>=1.13",
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
  "argon2-cffi>=23.1",
  "pyjwt>=2.8",
  "structlog>=24.1",
  "redis>=5.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2", "pytest-asyncio>=0.23", "httpx>=0.27",
  "ruff>=0.4", "mypy>=1.10", "psycopg[binary]>=3.1",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["integration: needs real models/services"]
```

- [ ] **Step 3: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
node_modules/
.next/
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
*.egg-info/
docker/pgdata/
docker/miniodata/
```

- [ ] **Step 4: Write `Makefile`**

```makefile
.PHONY: up down api test lint type verify openapi

up: ; docker compose -f docker/docker-compose.yml up -d
down: ; docker compose -f docker/docker-compose.yml down
api: ; cd backend && uvicorn app.main:app --reload --port 8000
test: ; cd backend && pytest -q
lint: ; cd backend && ruff check . && ruff format --check .
type: ; cd backend && mypy app
verify: lint type test
openapi: ; cd backend && python -m app.scripts.export_openapi && cd ../frontend && npm run codegen
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: scaffold repo, tooling, makefile"
```

---

### Task 2: Postgres + Neo4j + Redis/MinIO compose stack

**Files:**
- Create: `docker/postgres/Dockerfile`, `docker/docker-compose.yml`, `.env.example`

- [ ] **Step 1: Write `docker/postgres/Dockerfile`**

```dockerfile
FROM postgres:16
RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
# pgvector preloaded; extension created per-database by migration/init
COPY init-extensions.sql /docker-entrypoint-initdb.d/01-init-extensions.sql
```

- [ ] **Step 2: Write `docker/postgres/init-extensions.sql`**

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

- [ ] **Step 3: Write `docker/docker-compose.yml`**

```yaml
services:
  postgres:
    build: ./postgres
    environment:
      POSTGRES_USER: kb
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-kb_dev_password}
      POSTGRES_DB: kb
    ports: ["5432:5432"]
    volumes: ["./pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kb"]
      interval: 5s
      retries: 10

  neo4j:
    image: neo4j:5-community
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-kb_dev_password}
      NEO4J_PLUGINS: '["apoc"]'
      NEO4J_server_memory_pagecache_size: 512M
      NEO4J_server_memory_heap_initial__size: 512M
      NEO4J_server_memory_heap_max__size: 1G
    ports: ["7687:7687", "7474:7474"]
    volumes: ["./neo4jdata:/data", "./neo4jlogs:/logs"]
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:7474 || exit 1"]
      interval: 10s
      retries: 10

  redis:
    image: redis:7
    ports: ["6379:6379"]

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: kb
      MINIO_ROOT_PASSWORD: ${MINIO_PASSWORD:-kb_dev_password}
    ports: ["9000:9000", "9001:9001"]
    volumes: ["./miniodata:/data"]
```

- [ ] **Step 4: Write `.env.example`**

```env
DATABASE_URL=postgresql+asyncpg://kb:kb_dev_password@localhost:5432/kb
REDIS_URL=redis://localhost:6379/0
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=kb_dev_password
JWT_SECRET=change-me-in-prod
JWT_ACCESS_TTL_SECONDS=900
JWT_REFRESH_TTL_SECONDS=604800
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=kb
MINIO_SECRET_KEY=kb_dev_password
```

- [ ] **Step 5: Verify the stack**

```bash
make up && sleep 20
# Postgres: pgvector present
docker compose -f docker/docker-compose.yml exec postgres \
  psql -U kb -d kb -c "SELECT extname FROM pg_extension;"
# Expected: row 'vector'

# Neo4j: Bolt reachable
docker compose -f docker/docker-compose.yml exec neo4j \
  cypher-shell -u neo4j -p kb_dev_password "RETURN 1 AS ok;"
# Expected: ok = 1
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore(docker): postgres+pgvector, neo4j community, redis, minio"
```

---

### Task 3: FastAPI skeleton — settings, app factory, health, errors

**Files:**
- Create: `backend/app/core/config.py`, `backend/app/core/errors.py`, `backend/app/main.py`
- Test: `backend/tests/test_health.py`

- [ ] **Step 1: Write the failing test `backend/tests/test_health.py`**

```python
import httpx
import pytest
from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_healthz_returns_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run it — verify FAIL**

```bash
cd backend && pip install -e ".[dev]" && pytest tests/test_health.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Write `backend/app/core/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://kb:kb_dev_password@localhost:5432/kb"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-secret"
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 604800


settings = Settings()
```

- [ ] **Step 4: Write `backend/app/core/errors.py`**

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    status_code = 500


class NotFoundError(DomainError):
    status_code = 404


class ForbiddenError(DomainError):
    status_code = 403


class ConflictError(DomainError):
    status_code = 409


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc) or exc.__class__.__name__})
```

- [ ] **Step 5: Write `backend/app/main.py`**

```python
from fastapi import FastAPI

from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    app = FastAPI(title="Knowledge Base API", version="0.1.0")
    register_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 6: Run test — verify PASS**

```bash
pytest tests/test_health.py -v
```
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "feat(api): app factory, settings, domain errors, healthz"
```

---

### Task 4: Database layer & test infrastructure

**Files:**
- Create: `backend/app/core/db.py`, `backend/tests/conftest.py`
- Test: `backend/tests/test_db.py`

- [ ] **Step 1: Write the failing test `backend/tests/test_db.py`**

```python
from sqlalchemy import text


async def test_db_session_executes(db) -> None:
    result = await db.execute(text("SELECT 1"))
    assert result.scalar_one() == 1


async def test_extensions_present(db) -> None:
    result = await db.execute(text("SELECT extname FROM pg_extension"))
    names = {row[0] for row in result}
    assert {"vector"} <= names
```

- [ ] **Step 2: Run — verify FAIL** (`fixture 'db' not found`)

```bash
pytest tests/test_db.py -v
```

- [ ] **Step 3: Write `backend/app/core/db.py`**

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
```

- [ ] **Step 4: Write `backend/tests/conftest.py`**

```python
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
```

- [ ] **Step 5: Run — verify PASS** (stack must be up: `make up`)

```bash
pytest tests/test_db.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(db): async engine, session dependency, transactional test fixtures"
```

---

### Task 5: Alembic baseline — users, groups, group_members

**Files:**
- Create: `backend/alembic.ini`, `backend/alembic/env.py`, `backend/app/models/__init__.py`, `backend/app/models/user.py`, `backend/app/models/group.py`
- Migration: `backend/alembic/versions/0001_users_groups.py` (autogenerated then reviewed)
- Test: `backend/tests/models/test_user_model.py`

- [ ] **Step 1: Write the failing test `backend/tests/models/test_user_model.py`**

```python
from app.models.user import Role, User


async def test_user_roundtrip(db) -> None:
    user = User(email="a@example.com", password_hash="x", display_name="A", role=Role.user)
    db.add(user)
    await db.flush()
    assert user.id is not None
    assert user.role is Role.user
    assert user.is_active is True
```

- [ ] **Step 2: Run — verify FAIL** (`No module named 'app.models.user'`)

- [ ] **Step 3: Write `backend/app/models/user.py`**

```python
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Role(str, enum.Enum):
    admin = "admin"
    user = "user"
    service = "service"


class Visibility(str, enum.Enum):
    private = "private"
    public = "public"
    shared = "shared"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.user)
    default_visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, name="visibility"), default=Visibility.private
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 4: Write `backend/app/models/group.py`**

```python
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class GroupRole(str, enum.Enum):
    member = "member"
    manager = "manager"


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[GroupRole] = mapped_column(Enum(GroupRole, name="group_role"), default=GroupRole.member)
```

- [ ] **Step 5: Write `backend/app/models/__init__.py`**

```python
from app.models.group import Group, GroupMember, GroupRole
from app.models.user import Role, User, Visibility

__all__ = ["Group", "GroupMember", "GroupRole", "Role", "User", "Visibility"]
```

- [ ] **Step 6: Init Alembic and generate the baseline**

```bash
cd backend && alembic init alembic
```
Edit `alembic/env.py`: set `target_metadata = Base.metadata` (import `from app.core.db import Base` and `import app.models  # noqa: F401`), and configure the URL from settings, converting async URL to sync for Alembic:
```python
from app.core.config import settings
config.set_main_option("sqlalchemy.url", settings.database_url.replace("+asyncpg", "+psycopg"))
```
Then:
```bash
alembic revision --autogenerate -m "users groups"
alembic upgrade head
```
Expected: migration file created; upgrade succeeds. Review the generated file: it must create `users`, `groups`, `group_members` and the three enums — delete any noise.

- [ ] **Step 7: Run test — verify PASS**

```bash
pytest tests/models/test_user_model.py -v
```

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "feat(db): users, groups, group_members models + baseline migration"
```

---

### Task 6: Password hashing & auth service

**Files:**
- Create: `backend/app/services/auth_service.py`
- Test: `backend/tests/services/test_auth_service.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest

from app.core.errors import ConflictError
from app.services import auth_service


async def test_register_and_authenticate(db) -> None:
    user = await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    assert user.password_hash != "s3cret!pw"  # hashed, never plaintext

    ok = await auth_service.authenticate(db, "a@example.com", "s3cret!pw")
    assert ok is not None and ok.id == user.id

    bad = await auth_service.authenticate(db, "a@example.com", "wrong")
    assert bad is None


async def test_register_duplicate_email_conflicts(db) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    with pytest.raises(ConflictError):
        await auth_service.register(db, email="a@example.com", password="other", display_name="B")
```

- [ ] **Step 2: Run — verify FAIL** (`No module named 'app.services.auth_service'`)

- [ ] **Step 3: Write `backend/app/services/auth_service.py`**

```python
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models.user import Role, User

_hasher = PasswordHasher()


async def register(
    db: AsyncSession, *, email: str, password: str, display_name: str, role: Role = Role.user
) -> User:
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError(f"email already registered: {email}")
    user = User(email=email, password_hash=_hasher.hash(password), display_name=display_name, role=role)
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if user is None:
        return None
    try:
        _hasher.verify(user.password_hash, password)
    except VerifyMismatchError:
        return None
    return user
```

- [ ] **Step 4: Run — verify PASS**, then **commit**

```bash
pytest tests/services/test_auth_service.py -v
git add -A && git commit -m "feat(auth): argon2 registration and authentication"
```

---

### Task 7: JWT issue/verify + login/refresh/logout endpoints

**Files:**
- Create: `backend/app/core/security.py`, `backend/app/schemas/auth.py`, `backend/app/api/v1/auth.py`
- Modify: `backend/app/main.py` (include router)
- Test: `backend/tests/api/test_auth_api.py`

- [ ] **Step 1: Write the failing test**

```python
from app.services import auth_service


async def test_login_returns_tokens(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    resp = await client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "s3cret!pw"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"access_token", "refresh_token", "token_type"}


async def test_login_bad_password_401(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    resp = await client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "no"})
    assert resp.status_code == 401


async def test_refresh_rotates(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    login = await client.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "s3cret!pw"})
    refresh = login.json()["refresh_token"]
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    assert resp.json()["access_token"]
```

- [ ] **Step 2: Run — verify FAIL** (404 on /api/v1/auth/login)

- [ ] **Step 3: Write `backend/app/core/security.py`**

```python
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import settings

ALGO = "HS256"


def _make(sub: str, role: str, ttl: int, kind: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": sub, "role": role, "kind": kind, "jti": str(uuid.uuid4()),
         "iat": now, "exp": now + timedelta(seconds=ttl)},
        settings.jwt_secret, algorithm=ALGO,
    )


def make_access_token(user_id: uuid.UUID, role: str) -> str:
    return _make(str(user_id), role, settings.jwt_access_ttl_seconds, "access")


def make_refresh_token(user_id: uuid.UUID, role: str) -> str:
    return _make(str(user_id), role, settings.jwt_refresh_ttl_seconds, "refresh")


def decode_token(token: str, expected_kind: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
    if payload.get("kind") != expected_kind:
        raise jwt.InvalidTokenError("wrong token kind")
    return payload
```

- [ ] **Step 4: Write `backend/app/schemas/auth.py`**

```python
from pydantic import BaseModel, EmailStr


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
```

- [ ] **Step 5: Write `backend/app/api/v1/auth.py`**

```python
import uuid

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_token, make_access_token, make_refresh_token
from app.schemas.auth import LoginIn, RefreshIn, TokensOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokensOut, summary="Log in", operation_id="login")
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)) -> TokensOut:
    user = await auth_service.authenticate(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")  # auth boundary, not domain
    return TokensOut(
        access_token=make_access_token(user.id, user.role.value),
        refresh_token=make_refresh_token(user.id, user.role.value),
    )


@router.post("/refresh", response_model=TokensOut, summary="Rotate tokens", operation_id="refreshTokens")
async def refresh(payload: RefreshIn) -> TokensOut:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    uid, role = uuid.UUID(claims["sub"]), claims["role"]
    return TokensOut(access_token=make_access_token(uid, role), refresh_token=make_refresh_token(uid, role))
```
(Refresh revocation list in Redis is added in Phase 7 hardening; noted in that plan.)

- [ ] **Step 6: Modify `backend/app/main.py`** — add inside `create_app()` after error registration:

```python
from app.api.v1.auth import router as auth_router
app.include_router(auth_router, prefix="/api/v1")
```

- [ ] **Step 7: Run — verify PASS**, **commit**

```bash
pytest tests/api/test_auth_api.py -v
git add -A && git commit -m "feat(auth): JWT login and refresh endpoints"
```

---

### Task 8: Viewer dependency + /users/me

**Files:**
- Create: `backend/app/core/deps.py`, `backend/app/schemas/user.py`, `backend/app/api/v1/users.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/api/test_users_me.py`

- [ ] **Step 1: Write the failing test**

```python
from app.services import auth_service


async def _login(client, email, password):
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def test_me_returns_profile(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    token = await _login(client, "a@example.com", "s3cret!pw")
    resp = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "a@example.com"
    assert "password_hash" not in body


async def test_me_unauthenticated_401(client) -> None:
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run — verify FAIL** (404)

- [ ] **Step 3: Write `backend/app/core/deps.py`**

```python
import uuid
from dataclasses import dataclass

import jwt as pyjwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_token
from app.models.group import GroupMember
from app.models.user import Role

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Viewer:
    user_id: uuid.UUID
    role: Role
    group_ids: frozenset[uuid.UUID]


async def get_current_viewer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Viewer:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        claims = decode_token(creds.credentials, "access")
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    uid = uuid.UUID(claims["sub"])
    rows = await db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == uid))
    return Viewer(user_id=uid, role=Role(claims["role"]), group_ids=frozenset(rows))


async def require_admin(viewer: Viewer = Depends(get_current_viewer)) -> Viewer:
    if viewer.role is not Role.admin:
        raise HTTPException(status_code=403, detail="admin required")
    return viewer
```

- [ ] **Step 4: Write `backend/app/schemas/user.py`**

```python
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.user import Role, Visibility


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: Role
    default_visibility: Visibility
    is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    display_name: str
    role: Role = Role.user
```

- [ ] **Step 5: Write `backend/app/api/v1/users.py`**

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_current_viewer
from app.core.errors import NotFoundError
from app.models.user import User
from app.schemas.user import UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="Current user profile", operation_id="getMe")
async def me(viewer: Viewer = Depends(get_current_viewer), db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await db.get(User, viewer.user_id)
    if user is None:
        raise NotFoundError("user not found")
    return UserOut.model_validate(user)
```
Include in `main.py`: `app.include_router(users_router, prefix="/api/v1")`.

- [ ] **Step 6: Run — verify PASS**, **commit**

```bash
pytest tests/api/test_users_me.py -v
git add -A && git commit -m "feat(auth): Viewer dependency and /users/me"
```

---

### Task 9: Admin users & groups CRUD

**Files:**
- Create: `backend/app/api/v1/admin/__init__.py`, `backend/app/api/v1/admin/users.py`, `backend/app/api/v1/admin/groups.py`, `backend/app/schemas/group.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/api/test_admin_users.py`, `backend/tests/api/test_admin_groups.py`

- [ ] **Step 1: Write the failing tests `backend/tests/api/test_admin_users.py`**

```python
from app.models.user import Role
from app.services import auth_service


async def _token(client, email, pw):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_admin_creates_and_lists_users(db, client) -> None:
    await auth_service.register(db, email="root@x.com", password="rootpw!123", display_name="Root", role=Role.admin)
    headers = await _token(client, "root@x.com", "rootpw!123")

    resp = await client.post("/api/v1/admin/users", headers=headers,
                             json={"email": "b@x.com", "password": "bpw!12345", "display_name": "B"})
    assert resp.status_code == 201

    resp = await client.get("/api/v1/admin/users?limit=50&offset=0", headers=headers)
    assert resp.status_code == 200
    emails = [u["email"] for u in resp.json()["items"]]
    assert "b@x.com" in emails


async def test_non_admin_gets_403(db, client) -> None:
    await auth_service.register(db, email="u@x.com", password="userpw!123", display_name="U")
    headers = await _token(client, "u@x.com", "userpw!123")
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 403
```

And `backend/tests/api/test_admin_groups.py`:

```python
from app.models.user import Role
from app.services import auth_service


async def _token(client, email, pw):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_admin_creates_group_and_adds_member(db, client) -> None:
    await auth_service.register(db, email="root@x.com", password="rootpw!123", display_name="Root", role=Role.admin)
    member = await auth_service.register(db, email="m@x.com", password="mpw!12345", display_name="M")
    headers = await _token(client, "root@x.com", "rootpw!123")

    resp = await client.post("/api/v1/admin/groups", headers=headers,
                             json={"name": "game-team", "description": "Game dev"})
    assert resp.status_code == 201
    gid = resp.json()["id"]

    resp = await client.post(f"/api/v1/admin/groups/{gid}/members", headers=headers,
                             json={"user_id": str(member.id), "role": "member"})
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/admin/groups/{gid}", headers=headers)
    assert str(member.id) in [m["user_id"] for m in resp.json()["members"]]
```

- [ ] **Step 2: Run — verify FAIL** (404s)

- [ ] **Step 3: Write `backend/app/schemas/group.py`**

```python
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.group import GroupRole


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupMemberIn(BaseModel):
    user_id: uuid.UUID
    role: GroupRole = GroupRole.member


class GroupMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: uuid.UUID
    role: GroupRole


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str


class GroupDetailOut(GroupOut):
    members: list[GroupMemberOut]
```

- [ ] **Step 4: Write `backend/app/api/v1/admin/users.py`**

```python
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserOut
from app.services import auth_service

router = APIRouter(prefix="/users", tags=["admin"])


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int


@router.post("", response_model=UserOut, status_code=201, summary="Create user", operation_id="adminCreateUser")
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await auth_service.register(
        db, email=payload.email, password=payload.password,
        display_name=payload.display_name, role=payload.role,
    )
    return UserOut.model_validate(user)


@router.get("", response_model=UserListOut, summary="List users", operation_id="adminListUsers")
async def list_users(
    limit: int = Query(50, le=100), offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> UserListOut:
    total = await db.scalar(select(func.count()).select_from(User))
    rows = await db.scalars(select(User).order_by(User.created_at).limit(limit).offset(offset))
    return UserListOut(items=[UserOut.model_validate(u) for u in rows], total=total or 0)
```

- [ ] **Step 5: Write `backend/app/api/v1/admin/groups.py`**

```python
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_current_viewer
from app.core.errors import NotFoundError
from app.models.group import Group, GroupMember
from app.schemas.group import GroupCreate, GroupDetailOut, GroupMemberIn, GroupMemberOut, GroupOut

router = APIRouter(prefix="/groups", tags=["admin"])


@router.post("", response_model=GroupOut, status_code=201, summary="Create group", operation_id="adminCreateGroup")
async def create_group(
    payload: GroupCreate,
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    group = Group(name=payload.name, description=payload.description, created_by=viewer.user_id)
    db.add(group)
    await db.flush()
    return GroupOut.model_validate(group)


@router.get("/{group_id}", response_model=GroupDetailOut, summary="Group detail", operation_id="adminGetGroup")
async def get_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> GroupDetailOut:
    group = await db.get(Group, group_id)
    if group is None:
        raise NotFoundError("group not found")
    members = (await db.scalars(select(GroupMember).where(GroupMember.group_id == group_id))).all()
    return GroupDetailOut(
        id=group.id, name=group.name, description=group.description,
        members=[GroupMemberOut.model_validate(m) for m in members],
    )


@router.post("/{group_id}/members", status_code=204, summary="Add member", operation_id="adminAddGroupMember")
async def add_member(group_id: uuid.UUID, payload: GroupMemberIn, db: AsyncSession = Depends(get_db)) -> None:
    if await db.get(Group, group_id) is None:
        raise NotFoundError("group not found")
    await db.merge(GroupMember(group_id=group_id, user_id=payload.user_id, role=payload.role))
    await db.flush()
```

- [ ] **Step 6: Write `backend/app/api/v1/admin/__init__.py`** and wire with admin gate

```python
from fastapi import APIRouter, Depends

from app.api.v1.admin.groups import router as groups_router
from app.api.v1.admin.users import router as users_router
from app.core.deps import require_admin

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
router.include_router(users_router)
router.include_router(groups_router)
```
In `main.py`: `app.include_router(admin_router, prefix="/api/v1")`.

- [ ] **Step 7: Run — verify PASS**, **commit**

```bash
pytest tests/api -v
git add -A && git commit -m "feat(admin): user and group management endpoints"
```

---

### Task 10: Seed script, CI, phase gate

**Files:**
- Create: `backend/app/scripts/__init__.py`, `backend/app/scripts/seed_admin.py`, `backend/app/scripts/export_openapi.py`, `.github/workflows/ci.yml`

- [ ] **Step 1: Write `backend/app/scripts/seed_admin.py`**

```python
"""Idempotent admin seeder: python -m app.scripts.seed_admin email password"""
import asyncio
import sys

from app.core.db import SessionLocal
from app.core.errors import ConflictError
from app.models.user import Role
from app.services import auth_service


async def main(email: str, password: str) -> None:
    async with SessionLocal() as db:
        try:
            await auth_service.register(db, email=email, password=password, display_name="Admin", role=Role.admin)
            await db.commit()
            print(f"admin created: {email}")
        except ConflictError:
            print(f"admin already exists: {email}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
```

- [ ] **Step 2: Write `backend/app/scripts/export_openapi.py`**

```python
"""Writes openapi.json for frontend codegen: python -m app.scripts.export_openapi"""
import json
from pathlib import Path

from app.main import create_app

Path("openapi.json").write_text(json.dumps(create_app().openapi(), indent=2))
print("wrote backend/openapi.json")
```

- [ ] **Step 3: Write `.github/workflows/ci.yml`**

```yaml
name: ci
on: [push, pull_request]
jobs:
  backend:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env: { POSTGRES_USER: kb, POSTGRES_PASSWORD: kb_dev_password, POSTGRES_DB: kb }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U kb" --health-interval 5s --health-retries 10
      neo4j:
        image: neo4j:5-community
        env: { NEO4J_AUTH: "neo4j/kb_dev_password" }
        ports: ["7687:7687"]
        options: >-
          --health-cmd "wget -q --spider http://localhost:7474 || exit 1"
          --health-interval 10s --health-retries 10
      redis:
        image: redis:7
        ports: ["6379:6379"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e "backend[dev]"
      - run: psql postgresql://kb:kb_dev_password@localhost/kb -c "CREATE EXTENSION IF NOT EXISTS vector;"
      - run: cd backend && ruff check . && ruff format --check .
      - run: cd backend && mypy app
      - run: cd backend && alembic upgrade head
      - run: cd backend && pytest -q
        env:
          DATABASE_URL: postgresql+asyncpg://kb:kb_dev_password@localhost:5432/kb
          NEO4J_URI: bolt://localhost:7687
          NEO4J_USER: neo4j
          NEO4J_PASSWORD: kb_dev_password
```

- [ ] **Step 4: Run the full gate**

```bash
make verify
```
Expected: lint, type, and tests all green.

- [ ] **Step 5: Demonstrate exit criteria end-to-end**

```bash
make up
cd backend && alembic upgrade head
python -m app.scripts.seed_admin admin@company.com 'Adm1n!ChangeMe'
uvicorn app.main:app --port 8000 &
curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@company.com","password":"Adm1n!ChangeMe"}'
```
Expected: JSON with `access_token`. Paste output as evidence.

- [ ] **Step 6: Commit, update status, PR**

```bash
git add -A && git commit -m "chore: seed script, openapi export, CI"
```
Update `docs/plans/README.md` Phase 0 → Done. Open PR for human review.
