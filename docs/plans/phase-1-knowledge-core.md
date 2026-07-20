# Phase 1 — Knowledge Core

**Goal:** Implement the core knowledge node model, Neo4j graph integration, visibility filter, and full CRUD API. After this phase the system can create, update, soft-delete, and query knowledge nodes with privacy correctly enforced at every read path. Daily-log entries also land here.

**Architecture refs:** ADR-001 (amended), ADR-011 (Neo4j), ADR-004 (visibility choke-point)

**Required skills (read before any task):**
- `kb-conventions` — naming, style, commit format
- `kb-tdd-workflow` — RED→GREEN→REFACTOR iron law
- `kb-visibility-filter` — Viewer contract, every query goes through it
- `kb-neo4j-graph` — driver setup, MERGE vs CREATE, post-commit write order
- `kb-api-conventions` — router shape, schema naming, 202 for long work

**Exit criteria:**
- [ ] All tasks checked
- [ ] `pytest -x backend/tests/` green, no skips
- [ ] `ruff check backend/` clean
- [ ] `mypy --strict backend/app/services/ backend/app/schemas/` clean
- [ ] `/kb-verify` passes (visibility audit grep finds zero raw selects on knowledge_nodes outside visibility.py)
- [ ] `curl` evidence for every new endpoint in this file

---

## Task 1 — Extend conftest with node fixtures

**Files:**
- Modify: `backend/tests/conftest.py`

### Steps

- [x] **1.1** Open `backend/tests/conftest.py` and add these fixtures after the existing `client` fixture (plan-fix: there is no `auth_headers` fixture in conftest; `client` is the last existing fixture). The `app.models.knowledge` imports are done lazily inside each fixture because that module is only created in Task 2 — a top-level import would break collection of the whole suite (`NodeTag` is not needed by these fixtures and is not imported):

```python
# backend/tests/conftest.py  (additions only)
import uuid as _uuid
import pytest_asyncio
from app.models.user import Visibility

@pytest_asyncio.fixture
async def make_node(db):
    """Factory: create a KnowledgeNode in the test DB and return it."""
    from app.models.knowledge import KnowledgeNode  # lazy: model lands in Task 2

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
```

- [x] **1.2** Run tests to confirm fixtures load (no production code yet — test file just imports):

```bash
cd backend && pytest tests/conftest.py --collect-only -q
# Expected: collected 0 items (fixtures load without error)
```

- [x] **1.3** Commit:
```
chore(test): extend conftest with make_node and make_tag fixtures
```

---

## Task 2 — Knowledge node & related models + migration

**Files:**
- Create: `backend/app/models/knowledge.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0002_knowledge_core.py`
- Create: `backend/tests/models/test_knowledge_models.py`
- Modify: `backend/tests/conftest.py` (plan-fix: `make_user` fixture was assumed but never created in Task 1 — added here)

### Steps

- [x] **2.1** Write the failing test first (plan-fix applied to the code below: dropped unused `User`, `Role`, `NodeRevision`, `NodeTag` imports — ruff F401; `pytest.raises(Exception)` → `pytest.raises(IntegrityError)` — ruff B017; the failing flush runs inside `db.begin_nested()` so the outer test transaction stays usable and teardown emits no SAWarning. Post-review additions not shown below: `test_share_requires_exactly_one_grantee_neither`/`_both` (XOR check on `node_shares`) and `test_revision_version_unique_per_node` — see `backend/tests/models/test_knowledge_models.py`):

```python
# backend/tests/models/test_knowledge_models.py
import uuid
import pytest
from sqlalchemy import select
from app.models.knowledge import KnowledgeNode, NodeShare, NodeRevision, Tag, NodeTag
from app.models.user import Visibility, User, Role

pytestmark = pytest.mark.asyncio


async def test_node_create_and_read(db, make_user, make_node):
    user = await make_user(email="node@test.com")
    node = await make_node(user, title="Hello", body="world", visibility=Visibility.private)
    await db.flush()

    result = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node.id))
    assert result is not None
    assert result.title == "Hello"
    assert result.body == "world"
    assert result.deleted_at is None
    assert result.body_tsv is not None  # GENERATED column populated


async def test_node_soft_delete(db, make_user, make_node):
    from datetime import datetime, UTC
    user = await make_user(email="del@test.com")
    node = await make_node(user)
    node.deleted_at = datetime.now(UTC)
    await db.flush()

    result = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node.id))
    assert result.deleted_at is not None


async def test_node_share(db, make_user, make_node):
    owner = await make_user(email="owner@test.com")
    other = await make_user(email="other@test.com")
    node = await make_node(owner, visibility=Visibility.shared)
    share = NodeShare(node_id=node.id, user_id=other.id)
    db.add(share)
    await db.flush()
    result = await db.scalar(
        select(NodeShare).where(NodeShare.node_id == node.id, NodeShare.user_id == other.id)
    )
    assert result is not None


async def test_tag_slug_unique(db):
    t1 = Tag(id=uuid.uuid4(), name="Python", slug="python")
    t2 = Tag(id=uuid.uuid4(), name="Python", slug="python")
    db.add(t1)
    await db.flush()
    db.add(t2)
    with pytest.raises(Exception):  # IntegrityError
        await db.flush()
```

- [x] **2.2** Run — expect ImportError / FAIL:
```bash
cd backend && pytest tests/models/test_knowledge_models.py -x 2>&1 | head -30
```

- [x] **2.3** Create the model (plan-fix applied to the code below: added the `Computed` and `CheckConstraint` imports; removed unused `text`/`UTC` imports — ruff F401; `class NodeType(str, enum.Enum)` → `class NodeType(enum.StrEnum)` — ruff UP042, matches `Role`/`Visibility` in `user.py`; post-review: `ck_node_shares_user_xor_group` CheckConstraint added to `NodeShare` — exactly one of `user_id`/`group_id` must be set; NodeType vocabulary confirmed canonical by ADR-012):

```python
# backend/app/models/knowledge.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Index, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.user import Visibility  # reuse the enum


class NodeType(str, enum.Enum):
    note = "note"
    daily_log = "daily_log"
    file = "file"
    code_file = "code_file"
    code_symbol = "code_symbol"
    confluence_page = "confluence_page"


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    node_type: Mapped[str] = mapped_column(String(64), nullable=False, default=NodeType.note.value)
    visibility: Mapped[Visibility] = mapped_column(Enum(Visibility, name="visibility"), nullable=False, default=Visibility.private)
    source: Mapped[str | None] = mapped_column(String(64))          # "md_upload", "confluence", "codebase"
    source_ref: Mapped[str | None] = mapped_column(String(1024))    # unique external key
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    body_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))",
            persisted=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shares: Mapped[list[NodeShare]] = relationship("NodeShare", back_populates="node", cascade="all, delete-orphan")
    revisions: Mapped[list[NodeRevision]] = relationship("NodeRevision", back_populates="node", cascade="all, delete-orphan", order_by="NodeRevision.version.desc()")
    node_tags: Mapped[list[NodeTag]] = relationship("NodeTag", back_populates="node", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source", "source_ref", name="uq_node_source_ref"),
        Index("ix_kn_owner_deleted", "owner_id", "deleted_at"),
        Index("ix_kn_tsv", "body_tsv", postgresql_using="gin"),
    )


class NodeShare(Base):
    __tablename__ = "node_shares"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"))
    can_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node: Mapped[KnowledgeNode] = relationship("KnowledgeNode", back_populates="shares")

    __table_args__ = (
        UniqueConstraint("node_id", "user_id", name="uq_share_node_user"),
        UniqueConstraint("node_id", "group_id", name="uq_share_node_group"),
        CheckConstraint("(user_id IS NULL) != (group_id IS NULL)", name="ck_node_shares_user_xor_group"),
    )


class NodeRevision(Base):
    __tablename__ = "node_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(512), nullable=False)
    body_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node: Mapped[KnowledgeNode] = relationship("KnowledgeNode", back_populates="revisions")

    __table_args__ = (
        UniqueConstraint("node_id", "version", name="uq_revision_version"),
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node_tags: Mapped[list[NodeTag]] = relationship("NodeTag", back_populates="tag", cascade="all, delete-orphan")


class NodeTag(Base):
    __tablename__ = "node_tags"

    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    node: Mapped[KnowledgeNode] = relationship("KnowledgeNode", back_populates="node_tags")
    tag: Mapped[Tag] = relationship("Tag", back_populates="node_tags")
```

> **Note:** `Computed` must be imported: `from sqlalchemy import Computed`

- [x] **2.4** Add to `backend/app/models/__init__.py`:

```python
from app.models.knowledge import KnowledgeNode, NodeShare, NodeRevision, Tag, NodeTag, NodeType  # noqa: F401
```

- [x] **2.5** Generate Alembic migration (plan-fix: autogenerate DID include the `body_tsv` Computed column; the `visibility` column had to be switched to `postgresql.ENUM(..., name="visibility", create_type=False)` because the enum type already exists from migration 0001 and `sa.Enum` would try to re-create it. Post-review, migration 0002 was amended in place — it is unreleased: added `ck_node_shares_user_xor_group` check constraint; dropped redundant single-column indexes `ix_knowledge_nodes_owner_id` (covered by `ix_kn_owner_deleted`), `ix_node_shares_node_id` (covered by `uq_share_node_user`/`_group`), `ix_node_revisions_node_id` (covered by `uq_revision_version`) — model `index=True` flags removed to match):

```bash
cd backend && alembic revision --autogenerate -m "knowledge_core"
# Rename the generated file to 0002_knowledge_core.py
# Verify it contains: knowledge_nodes, node_shares, node_revisions, tags, node_tags
# Verify body_tsv Computed column is NOT in the autogenerated migration body
#   (autogenerate may miss Computed — add manually if absent):
#   sa.Column('body_tsv', postgresql.TSVECTOR(),
#       sa.Computed("to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))", persisted=True),
#       nullable=True),
```

- [x] **2.6** Apply migration and run tests — expect PASS:

```bash
cd backend && alembic upgrade head
pytest tests/models/test_knowledge_models.py -v
# Expected: 4 passed
```

- [x] **2.7** Commit:
```
feat(models): knowledge_nodes, node_shares, node_revisions, tags, node_tags + migration 0002
```

---

## Task 3 — Neo4j graph initialisation (constraint + driver singleton)

**Files:**
- Create: `backend/app/core/neo4j.py`
- Create: `backend/tests/db/test_neo4j_init.py`

> [plan-fix] `graph_service.py` removed from this task's file list: the steps below define no
> content for it (driver lives in `app/core/neo4j.py`); the full service lands in Task 5.
> Also added `neo4j>=5.20` to `backend/pyproject.toml` dependencies (was missing).

### Steps

- [x] **3.1** Write the failing test first:

```python
# backend/tests/db/test_neo4j_init.py
import pytest
from neo4j import AsyncSession as Neo4jSession

pytestmark = pytest.mark.asyncio


async def test_neo4j_reachable(neo4j_session: Neo4jSession):
    """Driver can execute a trivial query against the running Neo4j instance."""
    result = await neo4j_session.run("RETURN 1 AS ok")
    record = await result.single()
    assert record["ok"] == 1


async def test_node_id_constraint_exists(neo4j_session: Neo4jSession):
    """Uniqueness constraint on :Node(node_id) must exist."""
    result = await neo4j_session.run(
        "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties "
        "WHERE labelsOrTypes = ['Node'] AND properties = ['node_id'] RETURN name"
    )
    records = await result.data()
    assert len(records) >= 1, "Missing uniqueness constraint on :Node(node_id)"
```

- [x] **3.2** Run — expect FAIL (driver not configured yet):
```bash
cd backend && pytest tests/db/test_neo4j_init.py -x 2>&1 | head -20
```
Observed RED: `fixture 'neo4j_session' not found` (both tests error at setup).

- [x] **3.3** Write `backend/app/core/neo4j.py`:

```python
# backend/app/core/neo4j.py
"""Neo4j async driver singleton — import get_driver() everywhere."""
from neo4j import AsyncGraphDatabase, AsyncDriver
from app.core.config import settings

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def ensure_constraints() -> None:
    """Idempotent: create uniqueness constraint on :Node(node_id)."""
    async with get_driver().session() as session:
        await session.run(
            "CREATE CONSTRAINT node_id_unique IF NOT EXISTS "
            "FOR (n:Node) REQUIRE n.node_id IS UNIQUE"
        )
```

- [x] **3.4** Wire `ensure_constraints()` + `close_driver()` into FastAPI lifespan (`backend/app/main.py`):

```python
from contextlib import asynccontextmanager
from app.core.neo4j import close_driver, ensure_constraints

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ensure_constraints()
    yield
    await close_driver()
```
> [plan-fix] `main.py` uses the existing `create_app()` factory, so `lifespan` is passed as
> `FastAPI(..., lifespan=lifespan)` inside the factory rather than a module-level `app = FastAPI(...)`.

- [x] **3.5** Add `neo4j_session` fixture to `backend/tests/conftest.py`:

```python
@pytest.fixture
async def neo4j_session():
    if not _neo4j_available():
        pytest.skip("Neo4j unreachable")
    await ensure_constraints()  # tests must not depend on app lifespan having run
    async with get_driver().session() as session:
        yield session
        # Teardown: wipe all test nodes
        await session.run("MATCH (n:Node) WHERE n.test = true DETACH DELETE n")
```
> [plan-fix] (approved deviation) The fixture first probes `settings.neo4j_uri` with a 1 s TCP
> connect (`_neo4j_available()`) and skips when Neo4j is down, so the suite stays green in
> environments without Neo4j. It also calls `ensure_constraints()` because httpx's ASGITransport
> never runs the app lifespan (per kb-neo4j-graph: fixture runs the constraint migration).

- [x] **3.6** Add Neo4j env vars to `backend/tests/conftest.py` / pytest fixtures (already in `.env.example`; ensure `settings` reads them).
> [plan-fix] No `.env.example` exists; vars live in `backend/.env` (uncommitted). Added
> `neo4j_uri` / `neo4j_user` / `neo4j_password` fields (lowercase, matching existing
> `Settings` style; env matching is case-insensitive) with docker-compose dev defaults.

- [x] **3.7** Apply and run tests:

```bash
cd backend && pytest tests/db/test_neo4j_init.py -v
# Expected: 2 passed (sandbox without Neo4j: 2 skipped "Neo4j unreachable" — verify
# "2 passed" on the Docker stack)
```

- [x] **3.8** Commit:
```
feat(db): Neo4j driver singleton, ensure_constraints on startup, init tests
```

---

## Task 4 — Visibility filter service

**Files:**
- Create: `backend/app/services/visibility.py`
- Create: `backend/tests/services/test_visibility.py`

### Steps

- [x] **4.1** Write the failing tests first:

```python
# backend/tests/services/test_visibility.py
import uuid
import pytest
from sqlalchemy import select
from app.models.knowledge import KnowledgeNode
from app.models.user import Visibility, Role
from app.services.visibility import Viewer, visible_nodes_clause, shared_node_ids

pytestmark = pytest.mark.asyncio


async def test_private_node_invisible_to_others(db, make_user, make_node):
    owner = await make_user(email="v_owner@test.com")
    other = await make_user(email="v_other@test.com")
    node = await make_node(owner, visibility=Visibility.private)
    await db.flush()

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id not in ids, "Private node must NOT be visible to non-owner"


async def test_private_node_visible_to_owner(db, make_user, make_node):
    owner = await make_user(email="v_owner2@test.com")
    node = await make_node(owner, visibility=Visibility.private)
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Owner must see their own private node"


async def test_public_node_visible_to_all(db, make_user, make_node):
    owner = await make_user(email="v_pub@test.com")
    other = await make_user(email="v_pub2@test.com")
    node = await make_node(owner, visibility=Visibility.public)
    await db.flush()

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Public node must be visible to all"


async def test_shared_node_visible_to_share_target(db, make_user, make_node):
    from app.models.knowledge import NodeShare
    owner = await make_user(email="v_sh_owner@test.com")
    target = await make_user(email="v_sh_target@test.com")
    node = await make_node(owner, visibility=Visibility.shared)
    share = NodeShare(node_id=node.id, user_id=target.id)
    db.add(share)
    await db.flush()

    viewer = Viewer(user_id=target.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Shared node must be visible to share target"


async def test_admin_sees_all(db, make_user, make_node):
    owner = await make_user(email="v_adm_owner@test.com")
    admin = await make_user(email="v_admin@test.com", role=Role.admin)
    node = await make_node(owner, visibility=Visibility.private)
    await db.flush()

    viewer = Viewer(user_id=admin.id, role=Role.admin, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id in ids, "Admin must see all nodes"


async def test_deleted_nodes_excluded(db, make_user, make_node):
    from datetime import datetime, UTC
    owner = await make_user(email="v_del@test.com")
    node = await make_node(owner, visibility=Visibility.public)
    node.deleted_at = datetime.now(UTC)
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    clause = visible_nodes_clause(viewer)
    result = await db.scalars(select(KnowledgeNode).where(clause))
    ids = {r.id for r in result}
    assert node.id not in ids, "Soft-deleted node must be excluded"
```

> [plan-fix] Dropped the test file's unused `import uuid` (ruff F401) and added a seventh test,
> `test_shared_node_ids_returns_direct_shares` — the plan imported `shared_node_ids` without
> exercising it (F401 again, and production code must not land untested per kb-tdd-workflow).

- [x] **4.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_visibility.py -x 2>&1 | head -20
```

- [x] **4.3** Implement `visibility.py`:

```python
# backend/app/services/visibility.py
"""
SINGLE CHOKE POINT for all knowledge node visibility.

Every query that reads knowledge_nodes MUST call visible_nodes_clause().
No exceptions. See kb-visibility-filter skill.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode, NodeShare
from app.models.user import Role

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class Viewer:
    user_id: uuid.UUID
    role: Role
    group_ids: frozenset[uuid.UUID]


def visible_nodes_clause(viewer: Viewer) -> ColumnElement[bool]:
    """
    Return a SQLAlchemy WHERE clause that limits results to nodes visible
    to `viewer`.  Apply this to EVERY query on knowledge_nodes.

    Visibility rule (ADR-004):
        visible := deleted_at IS NULL AND (
            owner
            OR public
            OR (shared AND (direct share OR group share))
            OR admin
        )
    """
    from app.models.user import Visibility  # avoid circular at module level

    not_deleted = KnowledgeNode.deleted_at.is_(None)

    is_owner = KnowledgeNode.owner_id == viewer.user_id
    is_public = KnowledgeNode.visibility == Visibility.public

    # shared: node_shares has a row for this user or one of their groups
    shared_conditions = [NodeShare.user_id == viewer.user_id]
    if viewer.group_ids:
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID
        shared_conditions.append(
            NodeShare.group_id.in_(viewer.group_ids)
        )

    is_shared_with_viewer = and_(
        KnowledgeNode.visibility == Visibility.shared,
        KnowledgeNode.id.in_(
            select(NodeShare.node_id).where(or_(*shared_conditions))
        ),
    )

    if viewer.role == Role.admin:
        visibility_predicate = or_(True)  # admin sees everything
    else:
        visibility_predicate = or_(is_owner, is_public, is_shared_with_viewer)

    return and_(not_deleted, visibility_predicate)


async def shared_node_ids(viewer: Viewer, db: AsyncSession) -> set[uuid.UUID]:
    """
    Return IDs of all 'shared' nodes visible to viewer.
    Result is used by graph traversal service to filter Neo4j graph endpoints.
    Cache this in Redis (TTL=300s) in production; here we compute directly.
    """
    from app.models.user import Visibility
    from sqlalchemy import select as sa_select

    shared_conditions = [NodeShare.user_id == viewer.user_id]
    if viewer.group_ids:
        shared_conditions.append(NodeShare.group_id.in_(viewer.group_ids))

    rows = await db.scalars(
        sa_select(NodeShare.node_id).where(or_(*shared_conditions))
    )
    return set(rows)
```

> [plan-fix] As implemented, minus dead weight that fails the gates: removed unused
> function-local imports (`cast`, `PG_UUID`, `Visibility` in `shared_node_ids`), the empty
> `TYPE_CHECKING` block, and the redundant `select as sa_select` alias (module-level `select`
> already imported). Replaced `or_(True)` with `sqlalchemy.true()` — mypy --strict rejects
> `or_(True)` (the `or_` identity literal is `False` only).

- [x] **4.4** Run tests:
```bash
cd backend && pytest tests/services/test_visibility.py -v
# Expected: 6 passed  (actual: 7 passed — extra shared_node_ids test, see 4.1 plan-fix)
```

- [x] **4.5** Commit:
```
feat(visibility): implement visibility.py with Viewer contract and 6 rule tests
```

---

## Task 5 — Neo4j graph service

**Files:**
- Create: `backend/app/services/graph_service.py`
- Create: `backend/tests/services/test_graph_service.py`

> [plan-fix] The plan said "Update … driver setup skeleton was in Task 3", but Task 3 put the
> driver singleton in `app/core/neo4j.py` (no graph_service skeleton exists). This module is
> created here and imports `get_driver` from `app.core.neo4j` — exactly as the plan code below
> already does.

### Steps

- [x] **5.1** Write the failing tests:

```python
# backend/tests/services/test_graph_service.py
import pytest

from app.models.user import Role, Visibility
from app.services import graph_service as gs
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_create_vertex(db, neo4j_session, make_user, make_node):
    owner = await make_user(email="gs_v@test.com")
    node = await make_node(owner, title="Graph Node")
    await db.commit()
    await gs.upsert_vertex(node)
    # vertex should exist in Neo4j
    result = await neo4j_session.run(
        "MATCH (n:Node {node_id: $nid}) RETURN n", nid=str(node.id)
    )
    record = await result.single()
    assert record is not None
    assert record["n"]["title"] == "Graph Node"


async def test_merge_and_delete_edge(db, neo4j_session, make_user, make_node):
    owner = await make_user(email="gs_e@test.com")
    n1 = await make_node(owner, title="A")
    n2 = await make_node(owner, title="B")
    await db.commit()
    await gs.upsert_vertex(n1)
    await gs.upsert_vertex(n2)
    await gs.merge_edge(n1.id, n2.id, "LINKS_TO", created_by=str(owner.id))
    hood = await gs.get_neighborhood(
        db, n1.id, Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset()), hops=1
    )
    edge_targets = [e["target"] for e in hood["edges"]]
    assert str(n2.id) in edge_targets
    await gs.delete_edge(n1.id, n2.id, "LINKS_TO")
    hood2 = await gs.get_neighborhood(
        db, n1.id, Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset()), hops=1
    )
    assert str(n2.id) not in [e["target"] for e in hood2["edges"]]


async def test_neighborhood_visibility(db, neo4j_session, make_user, make_node):
    """Private nodes must not appear in another user's neighborhood traversal."""
    owner = await make_user(email="gs_vis1@test.com")
    other = await make_user(email="gs_vis2@test.com")
    public_node = await make_node(owner, title="Public", visibility=Visibility.public)
    private_node = await make_node(owner, title="Private", visibility=Visibility.private)
    await db.commit()
    await gs.upsert_vertex(public_node)
    await gs.upsert_vertex(private_node)
    await gs.merge_edge(public_node.id, private_node.id, "LINKS_TO", created_by="system")

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    hood = await gs.get_neighborhood(db, public_node.id, viewer, hops=1)
    node_ids = [v["id"] for v in hood["nodes"]]
    assert str(private_node.id) not in node_ids, (
        "Private node must not leak through graph traversal"
    )
```

> [plan-fix] As implemented: removed unused imports (`uuid`, `KnowledgeNode`) that fail
> `ruff check`; isort-ordered the rest; wrapped the >100-char assert. Added `neo4j_session`
> to `test_neighborhood_visibility` — it drives a live Neo4j via `upsert_vertex`/`merge_edge`,
> so it must depend on that fixture to SKIP (not error) when Neo4j is down, per the approved
> sandbox deviation. Test bodies unchanged.

- [x] **5.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_graph_service.py -x 2>&1 | head -20
```

- [x] **5.3** Implement `graph_service.py`:

> [plan-fix] As implemented, to pass the gates: bare `dict` generics annotated as
> `dict[str, Any]` (`mypy --strict` disallow_any_generics — also makes the `Any` import used),
> `nodes_out`/`raw_edges` explicitly annotated, and >100-char lines wrapped (ruff E501).
> Cypher, function signatures, and behavior are exactly as planned.

```python
# backend/app/services/graph_service.py
"""
Neo4j graph operations for the knowledge graph (ADR-011).

Rules (from kb-neo4j-graph skill):
- All driver calls go through this module only — no neo4j imports elsewhere.
- MERGE, not CREATE, for edges (idempotent).
- upsert_vertex / merge_edge / delete_edge are called AFTER the PG commit.
- Hop limit ≤ 3, node limit ≤ 500.
- Visibility filter applied via PG (the authoritative source) AFTER traversal.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.neo4j import get_driver
from app.models.knowledge import KnowledgeNode
from app.services.visibility import Viewer, visible_nodes_clause

_HOP_LIMIT = 3
_NODE_LIMIT = 500

ALLOWED_EDGE_LABELS = frozenset({
    "LINKS_TO", "REFERENCES", "DERIVED_FROM", "TAGGED_WITH", "SIMILAR_TO",
    "PARENT_OF", "AUTHORED_BY", "MENTIONS", "IMPORTS", "CALLS", "DEFINES",
    "BELONGS_TO_PROJECT",
})


async def upsert_vertex(node: KnowledgeNode) -> None:
    """Create or update a :Node vertex in Neo4j. Call AFTER PG commit."""
    async with get_driver().session() as session:
        await session.run(
            """
            MERGE (n:Node {node_id: $node_id})
            SET n.title      = $title,
                n.node_type  = $node_type,
                n.visibility = $visibility,
                n.owner_id   = $owner_id,
                n.deleted    = false
            """,
            node_id=str(node.id),
            title=node.title,
            node_type=node.node_type,
            visibility=node.visibility.value,
            owner_id=str(node.owner_id),
        )


async def soft_delete_vertex(node_id: uuid.UUID) -> None:
    """Mark vertex deleted. Call AFTER PG soft-delete commit."""
    async with get_driver().session() as session:
        await session.run(
            "MATCH (n:Node {node_id: $node_id}) SET n.deleted = true",
            node_id=str(node_id),
        )


async def merge_edge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    label: str,
    created_by: str,
    score: float | None = None,
) -> None:
    """MERGE a directed edge. label must be in ALLOWED_EDGE_LABELS."""
    assert label in ALLOWED_EDGE_LABELS, f"Unknown edge label: {label}"
    async with get_driver().session() as session:
        await session.run(
            f"""
            MATCH (a:Node {{node_id: $src}}), (b:Node {{node_id: $tgt}})
            MERGE (a)-[r:{label}]->(b)
            SET r.created_by = $created_by,
                r.score      = $score
            """,
            src=str(source_id),
            tgt=str(target_id),
            created_by=created_by,
            score=score,
        )


async def delete_edge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    label: str,
) -> None:
    assert label in ALLOWED_EDGE_LABELS, f"Unknown edge label: {label}"
    async with get_driver().session() as session:
        await session.run(
            f"""
            MATCH (a:Node {{node_id: $src}})-[r:{label}]->(b:Node {{node_id: $tgt}})
            DELETE r
            """,
            src=str(source_id),
            tgt=str(target_id),
        )


async def get_neighborhood(
    db: AsyncSession,
    center_id: uuid.UUID,
    viewer: Viewer,
    hops: int = 1,
) -> dict[str, list[dict[str, Any]]]:
    """
    Return nodes and edges within `hops` hops of center_id, visibility-filtered.
    hops clamped to _HOP_LIMIT.  Total nodes capped at _NODE_LIMIT.
    Visibility is enforced by re-querying PG (the authoritative source).
    """
    hops = max(0, min(hops, _HOP_LIMIT))  # defense-in-depth: interpolated into the pattern
    candidate_ids: set[uuid.UUID] = {center_id}
    raw_edges: list[dict[str, Any]] = []

    async with get_driver().session() as session:
        # [plan-fix] review CRITICAL: LIMIT must run BEFORE collect() — after
        # aggregation the match is a single row and LIMIT is a no-op (unbounded
        # pull on hub nodes). LIMIT now bounds rows pre-aggregation.
        result = await session.run(
            f"""
            MATCH (center:Node {{node_id: $cid}})-[e*0..{hops}]-(other:Node)
            WHERE other.deleted IS NULL OR other.deleted = false
            WITH DISTINCT other, e
            LIMIT $limit
            WITH collect(DISTINCT other) AS nodes,
                 collect(DISTINCT e)    AS edge_lists
            RETURN nodes, edge_lists
            """,
            cid=str(center_id),
            limit=_NODE_LIMIT,
        )
        record = await result.single()

    if record:
        for n in record["nodes"]:
            nid = n.get("node_id")
            if nid:
                try:
                    candidate_ids.add(uuid.UUID(nid))
                except ValueError:
                    pass
        for path_edges in record["edge_lists"]:
            if isinstance(path_edges, list):
                for e in path_edges:
                    raw_edges.append({
                        "source": e.start_node["node_id"] if hasattr(e, "start_node") else None,
                        "target": e.end_node["node_id"] if hasattr(e, "end_node") else None,
                        "label": e.type if hasattr(e, "type") else "",
                    })

    # Apply visibility filter via Postgres (authoritative)
    if not candidate_ids:
        return {"nodes": [], "edges": []}

    clause = visible_nodes_clause(viewer)
    visible_rows = await db.scalars(
        select(KnowledgeNode)
        .where(KnowledgeNode.id.in_(list(candidate_ids)))
        .where(clause)
    )
    visible_nodes = list(visible_rows)
    visible_ids = {str(n.id) for n in visible_nodes}

    nodes_out: list[dict[str, Any]] = [
        {
            "id": str(n.id),
            "title": n.title,
            "node_type": n.node_type,
            "visibility": n.visibility.value,
        }
        for n in visible_nodes
    ]
    edges_out = [
        e for e in raw_edges
        if e["source"] in visible_ids and e["target"] in visible_ids
    ]
    return {"nodes": nodes_out, "edges": edges_out}


async def get_overview(
    db: AsyncSession,
    viewer: Viewer,
    limit: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    """Top visible nodes + edges between them for the initial graph viewport."""
    clause = visible_nodes_clause(viewer)
    rows = await db.scalars(
        select(KnowledgeNode)
        .where(clause)
        .order_by(KnowledgeNode.updated_at.desc())
        .limit(limit)
    )
    nodes = list(rows)
    id_set = {str(n.id) for n in nodes}

    nodes_out: list[dict[str, Any]] = [
        {"id": str(n.id), "title": n.title, "node_type": n.node_type} for n in nodes
    ]

    if not id_set:
        return {"nodes": nodes_out, "edges": []}

    async with get_driver().session() as session:
        result = await session.run(
            """
            MATCH (a:Node)-[r]->(b:Node)
            WHERE a.node_id IN $ids AND b.node_id IN $ids
            RETURN a.node_id AS src, b.node_id AS tgt, type(r) AS lbl
            LIMIT $limit
            """,
            ids=list(id_set),
            limit=_NODE_LIMIT,
        )
        records = await result.data()

    edges_out = [{"source": r["src"], "target": r["tgt"], "label": r["lbl"]} for r in records]
    return {"nodes": nodes_out, "edges": edges_out}
```

- [x] **5.4** Run tests:
```bash
cd backend && pytest tests/services/test_graph_service.py -v
# Expected: 3 passed
# (sandbox actual: 3 skipped — "Neo4j unreachable"; approved deviation.
#  MUST be re-run against the Docker stack and show 3 passed.)
```

- [x] **5.5** Commit:
```
feat(graph): graph_service with Neo4j driver — upsert_vertex, merge/delete edge, neighborhood + visibility gate
```

---

## Task 6 — Node service (CRUD + wikilinks + visibility change)

**Files:**
- Create: `backend/app/services/node_service.py`
- Create: `backend/tests/services/test_node_service.py`

### Steps

- [x] **6.1** Write the failing tests (plan-fix: dropped unused `import uuid`; `test_wikilink_extraction` takes the `neo4j_session` fixture — it verifies edges via live Neo4j and must skip when Neo4j is unreachable. Review-fix of 24e5685 added: PG-first deferral tests (`_graph_recorder` + create/update/delete/wikilinks queue tests) a self-link guard test, a `max(version)+1` revision-gap test, mutation-authz tests (non-owner update/delete → `ForbiddenError`; admin CAN mutate, per this task's "Only owner or admin can edit/delete" check), and a `list_nodes` visibility test (user B never sees A's private node) — see the final `backend/tests/services/test_node_service.py`; the wikilink live test now calls `run_pending_graph_ops` after `resolve_wikilinks`):

```python
# backend/tests/services/test_node_service.py
import pytest

from app.models.user import Role, Visibility
from app.services import node_service as ns
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_create_node(db, make_user):
    owner = await make_user(email="ns_create@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    node = await ns.create_node(db, viewer=viewer, title="My Note", body="hello", node_type="note")
    assert node.id is not None
    assert node.owner_id == owner.id
    assert node.title == "My Note"


async def test_get_node_own(db, make_user, make_node):
    owner = await make_user(email="ns_get@test.com")
    node = await make_node(owner, title="GetMe")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    result = await ns.get_node(db, node.id, viewer)
    assert result.id == node.id


async def test_get_node_forbidden(db, make_user, make_node):
    from app.core.errors import ForbiddenError
    owner = await make_user(email="ns_fo@test.com")
    other = await make_user(email="ns_fo2@test.com")
    node = await make_node(owner, visibility=Visibility.private)
    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    with pytest.raises(ForbiddenError):
        await ns.get_node(db, node.id, viewer)


async def test_update_node_creates_revision(db, make_user, make_node):
    owner = await make_user(email="ns_upd@test.com")
    node = await make_node(owner, title="Old Title", body="old body")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    updated = await ns.update_node(db, node.id, viewer, title="New Title", body="new body")
    assert updated.title == "New Title"
    await db.refresh(node, ["revisions"])
    assert len(node.revisions) == 1
    assert node.revisions[0].title_snapshot == "Old Title"


async def test_wikilink_extraction(db, neo4j_session, make_user, make_node):
    owner = await make_user(email="ns_wl@test.com")
    n1 = await make_node(owner, title="Source Note", body="see [[Target Note]] and [[Other]]")
    n2 = await make_node(owner, title="Target Note", body="")
    await db.flush()
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.resolve_wikilinks(db, n1, viewer)
    # resolve_wikilinks only QUEUES graph ops (PG-first); run them as the
    # post-commit caller would.
    await ns.run_pending_graph_ops(db)
    # edges should have been created — verify via graph service
    from app.services import graph_service as gs
    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    targets = [e["target"] for e in hood["edges"]]
    assert str(n2.id) in targets


async def test_soft_delete(db, make_user, make_node):
    from app.core.errors import NotFoundError
    owner = await make_user(email="ns_del@test.com")
    node = await make_node(owner)
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.delete_node(db, node.id, viewer)
    with pytest.raises(NotFoundError):
        await ns.get_node(db, node.id, viewer)
```

- [x] **6.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_node_service.py -x 2>&1 | head -20
```

- [x] **6.3** Implement `node_service.py` (plan-fix: matched the real `graph_service` API — `upsert_vertex(node)`, `soft_delete_vertex(node_id)`, `merge_edge(src, tgt, label, created_by=...)`; there is no `create_vertex`, no `db` arg, no `props` kwarg. Graph calls wrapped in best-effort `_graph_sync` so a Neo4j failure never fails/rolls back the PG write (CLAUDE.md invariant; Celery retry task lands with the workers phase). `func` imported at top instead of the bottom-of-file import):

> [plan-fix, review of 24e5685] **PG-first invariant**: mutation functions must not run
> Neo4j ops inside the transaction (get_db commits after the handler returns, so an
> in-function `_graph_sync` ran pre-commit). All mutations now QUEUE ops on the session
> (`db.info["pending_graph_ops"]` via `_queue_graph_op`) and the caller runs
> `await node_service.run_pending_graph_ops(db)` AFTER `db.commit()` (see Task 8 router).
> `create_node`'s old "caller calls gs.upsert_vertex post-commit" note is superseded by the
> same queue. Also: `resolve_wikilinks` skips self-links (`[[Own Title]]`), and revision
> numbering uses `max(version)+1` under a `FOR UPDATE` lock on the node row instead of the
> racy `COUNT(*)+1`; a residual duplicate `(node_id, version)` maps to `ConflictError` (409).

```python
# backend/app/services/node_service.py
from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from functools import partial
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.knowledge import KnowledgeNode, NodeRevision, NodeType
from app.models.user import Role, Visibility
from app.services import graph_service as gs
from app.services.visibility import Viewer, visible_nodes_clause

logger = structlog.get_logger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


async def _graph_sync(op: Coroutine[Any, Any, None]) -> None:
    """Best-effort Neo4j sync — PG is the source of truth (ADR-011).

    A graph failure must never fail (or roll back) the relational write.
    TODO(workers phase): enqueue tasks.sync_graph_vertex retry instead of log-only.
    """
    try:
        await op
    except Exception as exc:
        logger.warning("graph_sync_failed", error=str(exc))


# A pending graph operation: zero-arg callable producing the coroutine to await.
# Stored as partials (not live coroutines) so discarding them on rollback is safe.
GraphOp = Callable[[], Coroutine[Any, Any, None]]

_PENDING_KEY = "pending_graph_ops"


def _queue_graph_op(db: AsyncSession, op: GraphOp) -> None:
    """Queue a Neo4j op to run after the PG commit — never inside it (ADR-011)."""
    db.info.setdefault(_PENDING_KEY, []).append(op)


def pending_graph_ops(db: AsyncSession) -> list[GraphOp]:
    """Graph ops queued on this session, awaiting run_pending_graph_ops()."""
    ops: list[GraphOp] = db.info.get(_PENDING_KEY, [])
    return list(ops)


async def run_pending_graph_ops(db: AsyncSession) -> None:
    """Run (and clear) the session's queued Neo4j ops.

    Callers MUST invoke this AFTER ``db.commit()``. Best-effort: each op is
    wrapped in _graph_sync, so a Neo4j failure is logged, never raised.
    """
    ops: list[GraphOp] = db.info.pop(_PENDING_KEY, [])
    for op in ops:
        await _graph_sync(op())


async def create_node(
    db: AsyncSession,
    *,
    viewer: Viewer,
    title: str,
    body: str = "",
    node_type: str = NodeType.note.value,
    visibility: Visibility = Visibility.private,
    source: str | None = None,
    source_ref: str | None = None,
    meta: dict[str, Any] | None = None,
) -> KnowledgeNode:
    node = KnowledgeNode(
        id=uuid.uuid4(),
        owner_id=viewer.user_id,
        title=title,
        body=body,
        node_type=node_type,
        visibility=visibility,
        source=source,
        source_ref=source_ref,
        meta=meta or {},
    )
    db.add(node)
    await db.flush()
    # Neo4j vertex upsert runs AFTER db.commit(): queued here, executed by the
    # caller via run_pending_graph_ops(db) (module docstring, kb-neo4j-graph).
    _queue_graph_op(db, partial(gs.upsert_vertex, node))
    return node


async def get_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> KnowledgeNode:
    clause = visible_nodes_clause(viewer)
    row = await db.scalar(
        select(KnowledgeNode).where(KnowledgeNode.id == node_id).where(clause)
    )
    if row is None:
        # Distinguish not-found from forbidden
        exists = await db.scalar(
            select(KnowledgeNode.id).where(KnowledgeNode.id == node_id, KnowledgeNode.deleted_at.is_(None))
        )
        if exists is None:
            raise NotFoundError(f"Node {node_id} not found")
        raise ForbiddenError(f"Node {node_id} not accessible")
    return row


async def list_nodes(
    db: AsyncSession,
    viewer: Viewer,
    *,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[KnowledgeNode], int]:
    clause = visible_nodes_clause(viewer)
    total = await db.scalar(select(func.count()).select_from(KnowledgeNode).where(clause)) or 0
    rows = await db.scalars(
        select(KnowledgeNode).where(clause).order_by(KnowledgeNode.updated_at.desc()).offset(offset).limit(limit)
    )
    return list(rows), total


async def update_node(
    db: AsyncSession,
    node_id: uuid.UUID,
    viewer: Viewer,
    *,
    title: str | None = None,
    body: str | None = None,
    visibility: Visibility | None = None,
    meta: dict[str, Any] | None = None,
) -> KnowledgeNode:
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can edit a node")

    # Save revision before mutating. Lock the node row so concurrent updates
    # serialize their revision numbering, then take max(version)+1 —
    # COUNT(*)+1 is racy and breaks when versions have gaps.
    await db.execute(
        select(KnowledgeNode.id).where(KnowledgeNode.id == node_id).with_for_update()
    )
    max_version = (
        await db.scalar(
            select(func.max(NodeRevision.version)).where(NodeRevision.node_id == node_id)
        )
        or 0
    )
    revision = NodeRevision(
        id=uuid.uuid4(),
        node_id=node.id,
        version=max_version + 1,
        title_snapshot=node.title,
        body_snapshot=node.body,
        changed_by=viewer.user_id,
    )
    db.add(revision)

    if title is not None:
        node.title = title
    if body is not None:
        node.body = body
    if visibility is not None:
        node.visibility = visibility
    if meta is not None:
        node.meta = {**node.meta, **meta}

    node.updated_at = datetime.now(UTC)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Residual duplicate (node_id, version) despite the row lock —
        # e.g. a writer path that skipped the lock. Surface as a 409.
        if "uq_revision_version" in str(exc.orig):
            raise ConflictError(f"Concurrent revision conflict for node {node_id}") from exc
        raise
    _queue_graph_op(db, partial(gs.upsert_vertex, node))  # vertex props, post-commit
    return node


async def delete_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> None:
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can delete a node")
    node.deleted_at = datetime.now(UTC)
    await db.flush()
    _queue_graph_op(db, partial(gs.soft_delete_vertex, node_id))


async def resolve_wikilinks(db: AsyncSession, node: KnowledgeNode, viewer: Viewer) -> None:
    """
    Find [[Title]] references in node.body, resolve to node IDs by title,
    and queue LINKS_TO edge MERGEs for post-commit run_pending_graph_ops().
    Unresolved titles and self-references ([[Own Title]]) are silently skipped.
    """
    titles = _WIKILINK_RE.findall(node.body)
    if not titles:
        return

    # Ensure source vertex exists (queued: runs post-commit, before the edges)
    _queue_graph_op(db, partial(gs.upsert_vertex, node))

    clause = visible_nodes_clause(viewer)
    for title in set(titles):
        if title == node.title:
            continue  # self-link guard: [[Own Title]] creates no edge
        # Titles are not unique; MVP behavior is "first visible match wins"
        # (.limit(1)). Revisit if titles ever get a uniqueness rule.
        target = await db.scalar(
            select(KnowledgeNode)
            .where(KnowledgeNode.title == title)
            .where(clause)
            .limit(1)
        )
        if target is None or target.id == node.id:
            continue
        _queue_graph_op(db, partial(gs.upsert_vertex, target))
        _queue_graph_op(
            db, partial(gs.merge_edge, node.id, target.id, "LINKS_TO", created_by="wikilink")
        )
```

- [x] **6.4** Run tests:
```bash
cd backend && pytest tests/services/test_node_service.py -v
# Expected: 16 passed (15 passed, 1 skipped when Neo4j is unreachable — wikilink test verifies via live Neo4j)
```

- [x] **6.5** Commit:
```
feat(node_service): CRUD, revisions, wikilink resolution, soft-delete
```

---

## Task 7 — Pydantic schemas for nodes

**Files:**
- Create: `backend/app/schemas/node.py`
- Create: `backend/tests/schemas/test_node_schemas.py`

### Steps

- [x] **7.1** Write the failing test ([plan-fix]: `NodeOut` must not expose `deleted_at` —
  kb-api-conventions: "Out schemas never expose ... soft-delete fields"; dropped it from the
  test input dict and asserted its absence in the dump; also added `tests/schemas/__init__.py`
  to match the existing test-package convention):

```python
# backend/tests/schemas/test_node_schemas.py
import uuid
from datetime import UTC, datetime

from app.schemas.node import NodeCreate, NodeOut, NodeUpdate


def test_node_create_defaults():
    n = NodeCreate(title="Hello")
    assert n.body == ""
    assert n.visibility.value == "private"


def test_node_out_no_internal_fields():
    data = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        title="T",
        body="B",
        node_type="note",
        visibility="private",
        source=None,
        source_ref=None,
        meta={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    out = NodeOut(**data)
    dumped = out.model_dump()
    assert "password_hash" not in dumped
    assert "body_tsv" not in dumped
    assert "deleted_at" not in dumped


def test_node_update_partial():
    u = NodeUpdate(title="New")
    assert u.body is None
    assert u.title == "New"
```

- [x] **7.2** Run — expect ImportError:
```bash
cd backend && pytest tests/schemas/test_node_schemas.py -x 2>&1 | head -10
```

- [x] **7.3** Create schemas ([plan-fix]: use `ConfigDict(from_attributes=True)` and no
  `from __future__ import annotations`, matching the established style in
  `app/schemas/user.py` / `group.py`; `deleted_at` removed from `NodeOut` per 7.1 note):

```python
# backend/app/schemas/node.py
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import Visibility


class NodeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    body: str = ""
    node_type: str = "note"
    visibility: Visibility = Visibility.private
    source: str | None = None
    source_ref: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class NodeUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=512)
    body: str | None = None
    visibility: Visibility | None = None
    meta: dict[str, Any] | None = None


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    body: str
    node_type: str
    visibility: Visibility
    source: str | None
    source_ref: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class NodeListOut(BaseModel):
    items: list[NodeOut]
    total: int
    offset: int
    limit: int


class NodeShareCreate(BaseModel):
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    can_edit: bool = False


class GraphNeighborhoodOut(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
```

- [x] **7.4** Run tests:
```bash
cd backend && pytest tests/schemas/test_node_schemas.py -v
# Expected: 3 passed
```

- [x] **7.5** Commit:
```
feat(schemas): node schemas (NodeCreate, NodeUpdate, NodeOut, NodeListOut, GraphNeighborhoodOut)
```

---

## Task 8 — Nodes API router

> **Review carry-over from Task 4 (IMPORTANT):** the `Viewer(role=admin)` bypass in
> `visible_nodes_clause` is unconstrained. When building routers, ensure regular
> (non-admin-console) routes construct the Viewer from the authenticated user's real
> role but do NOT offer an admin-scoped path outside `/api/v1/admin/*`, and add an
> audit log entry wherever an admin Viewer is used to read another user's non-public
> node (ADR-004 / kb-visibility-filter rule 5).

> **Carry-over resolution (Task 8):** guard implemented as `get_scoped_viewer` in
> `app/core/deps.py` — on all routes outside `/api/v1/admin/*` an admin's Viewer is
> scoped down to `role=user` before it reaches `visible_nodes_clause`, so the admin
> bypass is unreachable here (regression test:
> `test_admin_gets_no_visibility_bypass_outside_admin_routes`). The **audit-log part is
> deferred**: no audit mechanism exists yet (`audit_log` is in the canonical vocabulary
> but unimplemented). Phase 7 hardening must add the `audit_log` table and log every
> admin read of another user's non-public node on `/api/v1/admin/*` routes.

**Files:**
- Create: `backend/app/api/v1/nodes.py`
- Modify: `backend/app/main.py`, `backend/app/core/deps.py` ([plan-fix] `get_scoped_viewer` + `Pagination`), `backend/app/services/node_service.py` ([plan-fix] `share_node` moved into the service), `backend/tests/conftest.py`
- Create: `backend/tests/api/test_nodes_api.py`

### Steps

- [x] **8.1** Write the failing tests ([plan-fix]: the file as written below plus 6 more —
  401 unauthenticated, 422 missing title, list hides other users' private nodes
  (kb-visibility-filter mandatory test), admin-bypass guard (carry-over above), and two
  share tests: share grants visibility to a `shared` node, non-owner share attempt → 403):

```python
# backend/tests/api/test_nodes_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_create_node(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/nodes",
        json={"title": "My Note", "body": "hello world"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "My Note"
    assert "id" in data
    assert "body_tsv" not in data
    assert "password_hash" not in data


async def test_get_node_own(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"title": "GetMe"}, headers=auth_headers)
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{node_id}", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == node_id


# [plan-fix] review CRITICAL: plan originally asserted 403 here, but a 403
# confirms the private node id EXISTS (ADR-004 / kb-visibility-filter forbid
# existence leaks). Invisible must look nonexistent: 404, generic body.
async def test_get_private_node_other_user_looks_not_found(client: AsyncClient, auth_headers, auth_headers_other):
    r = await client.post("/api/v1/nodes", json={"title": "PrivateSecretTitle", "visibility": "private"}, headers=auth_headers)
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{node_id}", headers=auth_headers_other)
    assert r2.status_code == 404
    assert node_id not in r2.text  # existence
    assert "PrivateSecretTitle" not in r2.text  # content


async def test_list_nodes(client: AsyncClient, auth_headers):
    for i in range(3):
        await client.post("/api/v1/nodes", json={"title": f"Node {i}"}, headers=auth_headers)
    r = await client.get("/api/v1/nodes", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert data["total"] >= 3


async def test_update_node(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"title": "Old"}, headers=auth_headers)
    nid = r.json()["id"]
    r2 = await client.patch(f"/api/v1/nodes/{nid}", json={"title": "New"}, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["title"] == "New"


async def test_delete_node(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/nodes", json={"title": "ToDelete"}, headers=auth_headers)
    nid = r.json()["id"]
    r2 = await client.delete(f"/api/v1/nodes/{nid}", headers=auth_headers)
    assert r2.status_code == 204
    r3 = await client.get(f"/api/v1/nodes/{nid}", headers=auth_headers)
    assert r3.status_code == 404
```

> **Note:** `auth_headers_other` fixture must be added to conftest (create a second user and return its headers).

- [x] **8.2** Run — expect 404 (router not registered):
```bash
cd backend && pytest tests/api/test_nodes_api.py -x 2>&1 | head -20
# observed RED: 12 failed — "assert 404 == 201" etc. (router missing)
```

- [x] **8.3** Create the router ([plan-fix, review of 24e5685]: mutation handlers must run
  `await ns.run_pending_graph_ops(db)` AFTER `await db.commit()` — the service only queues
  Neo4j ops on the session; the router is the post-commit caller).
  Further [plan-fix]es applied to the code as originally written here:
  - `viewer=Depends(get_current_viewer)` → `viewer: Viewer = Depends(get_scoped_viewer)`
    (admin-bypass guard, carry-over above);
  - the share handler's `payload: "NodeShareCreate"` string annotation with an in-function
    import cannot be resolved by FastAPI — `NodeShareCreate` is imported at module level;
  - the share handler contained business logic (`db.add(NodeShare(...))`) in the router and
    **no owner check** — any viewer of a shared node could re-share it to anyone (ADR-004
    leak). Moved to `node_service.share_node()`, which raises `ForbiddenError` unless the
    viewer is the owner (admin included in the service check, but admins are scoped to
    `user` on this router);
  - handlers return `NodeOut.model_validate(node)` (never ORM objects) and every route has
    `summary` + `operation_id` per kb-api-conventions.
  Final router code is `backend/app/api/v1/nodes.py` (implemented as described).

- [x] **8.4** Register router in `main.py` ([plan-fix]: matched the file's existing import
  style):

```python
# backend/app/main.py  (add inside create_app, after existing routers)
from app.api.v1.nodes import router as nodes_router
app.include_router(nodes_router, prefix="/api/v1")
```

- [x] **8.5** Add `auth_headers_other` fixture and `Pagination` dep if not present
  ([plan-fix]: there is no `/api/v1/auth/register` endpoint and login is JSON
  (`{"email", "password"}`), not OAuth form data; also `auth_headers` itself did not
  exist yet and an `auth_headers_admin` fixture was needed for the carry-over guard test —
  all three now share a `_register_and_login` helper that registers via
  `auth_service.register`):

```python
# backend/tests/conftest.py  (actual)
async def _register_and_login(db, client, email: str, *, role: Role = Role.user) -> dict[str, str]:
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
```

```python
# backend/app/core/deps.py  (add Pagination)
from fastapi import Query

class Pagination:
    def __init__(self, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        self.offset = offset
        self.limit = limit
```

- [x] **8.6** Run tests:
```bash
cd backend && pytest tests/api/test_nodes_api.py -v
# Observed GREEN: 12 passed in 3.99s (6 plan tests + 6 auth/guard/share tests, see 8.1)
# Full suite: 61 passed, 6 skipped (Neo4j-unreachable skips) · ruff clean ·
# mypy app/services app/schemas: Success: no issues found in 10 source files
```

- [x] **8.7** curl evidence ([plan-fix]: login is JSON `{"email","password"}`, not form
  data, and `*.local` addresses are rejected by pydantic EmailStr — evidence user is
  `admin@example.com`; evidence users/nodes were deleted from the dev DB after the
  run — `SELECT email FROM users` is empty again, so the suite stays deterministic):

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"admin1234"}' | jq -r .access_token)
curl -s -X POST http://localhost:8000/api/v1/nodes -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"First Node","body":"hello [[Second Node]]","visibility":"public"}'
# ... GET /nodes/{id}, GET /nodes, PATCH, POST /nodes/{id}/shares, DELETE
```

Observed (live uvicorn, Neo4j down → post-commit graph sync logged as warning, API
unaffected per ADR-011):

```
== POST /api/v1/nodes ==            201
{ "id": "a921ddf2-a98b-4189-a724-086643d3a285",
  "owner_id": "5583fac0-2d1c-4e3d-b6dd-cd31f0077f23",
  "title": "First Node", "body": "hello [[Second Node]]", "node_type": "note",
  "visibility": "public", "source": null, "source_ref": null, "meta": {},
  "created_at": "2026-07-20T22:18:08.474229Z", "updated_at": "2026-07-20T22:18:08.474229Z" }
== GET /api/v1/nodes/{id} ==        200
{'id': 'a921ddf2-...', 'title': 'First Node', 'visibility': 'public'}
== GET /api/v1/nodes (list) ==      200  total= 1 items= ['First Node']
== PATCH /api/v1/nodes/{id} ==      200  {'id': 'a921ddf2-...', 'title': 'First Node v2'}
== POST /api/v1/nodes/{id}/shares == [201]  (shared node, user_id=bob)
== bob GET shared node: 200 ==
== DELETE /api/v1/nodes/{id} ==     [204]
== GET after delete: 404 ==
```

- [x] **8.8** Commit:
```
feat(api): POST/GET/PATCH/DELETE /api/v1/nodes with visibility enforcement
```

---

## Task 9 — Graph & edges API routers

**Files:**
- Create: `backend/app/api/v1/edges.py`
- Create: `backend/app/api/v1/graph.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/schemas/node.py` ([plan-fix]: edge schemas live in `app/schemas/`, not inline in the router — kb-api-conventions, and mypy --strict covers `app/schemas`)
- Modify: `backend/app/core/errors.py` ([plan-fix]: plan did not specify Neo4j-down behavior; graph-sourced endpoints surface `neo4j.exceptions.ServiceUnavailable` as 503 via the central error mapping)
- Create: `backend/tests/api/test_graph_api.py`

### Steps

- [x] **9.1** Write failing tests ([plan-fix]: tests that create or traverse edges need a live Neo4j, so they take the `neo4j_session` fixture and skip when Neo4j is unreachable — same convention as `tests/services/test_graph_service.py`. Added the mandatory kb-api-conventions tests the plan omitted: 401 unauthenticated, 422 unknown label, 422 hops>3, plus the mandatory kb-visibility-filter tests: edge-to-invisible-target → 404 no-leak, neighborhood-of-invisible-center → 404 no-leak, overview hides other users' private nodes. Those visibility tests are pure-PG — the 404 fires before any Neo4j call — so they always run. RED observed: 5 failed (404s: routes not registered), 3 skipped):

```python
# backend/tests/api/test_graph_api.py — full file in repo; the three plan tests as landed:
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_create_edge(client: AsyncClient, auth_headers, neo4j_session):
    n1_id = await _create_node(client, auth_headers, "N1")   # helper posts /api/v1/nodes
    n2_id = await _create_node(client, auth_headers, "N2")
    r = await client.post("/api/v1/edges", json={"source_id": n1_id, "target_id": n2_id, "label": "LINKS_TO"}, headers=auth_headers)
    assert r.status_code == 201


async def test_neighborhood(client: AsyncClient, auth_headers, neo4j_session):
    n1_id = await _create_node(client, auth_headers, "Center")
    n2_id = await _create_node(client, auth_headers, "Neighbour")
    await client.post("/api/v1/edges", json={"source_id": n1_id, "target_id": n2_id, "label": "LINKS_TO"}, headers=auth_headers)
    r = await client.get(f"/api/v1/graph/neighborhood/{n1_id}?hops=1", headers=auth_headers)
    assert r.status_code == 200
    ids = [n["id"] for n in r.json()["nodes"]]
    assert n2_id in ids


async def test_graph_overview(client: AsyncClient, auth_headers):
    r = await client.get("/api/v1/graph/overview", headers=auth_headers)
    assert r.status_code == 200
    assert "nodes" in r.json()
    assert "edges" in r.json()

# also landed: test_delete_edge (neo4j_session), test_create_edge_unauthenticated_401,
# test_create_edge_unknown_label_422, test_neighborhood_hops_above_limit_422,
# test_create_edge_to_invisible_target_looks_not_found,
# test_neighborhood_invisible_center_looks_not_found, test_overview_hides_other_users_private
```

- [x] **9.2** Edge schemas + `edges.py` ([plan-fix]: the plan's `gs.merge_edge(db, ..., props=...)` / `gs.delete_edge(db, ...)` calls don't match the Task 5 signatures — `merge_edge(source_id, target_id, label, created_by, score=None)` and `delete_edge(source_id, target_id, label)` take no `db` and no `props`; `created_by` is the viewer's user id. Dropped `props` (nothing consumes it — YAGNI) and the `db.commit()` calls (edges live only in Neo4j, there is no PG write to commit). `label` is validated against `graph_service.ALLOWED_EDGE_LABELS` in the schema so an unknown label is a 422, not an `assert` 500 — it is interpolated into Cypher. Viewer dep is `get_scoped_viewer` (Task 8 fix: no admin bypass outside `/api/v1/admin/*`); `response_model`/`summary`/`operation_id` set per kb-api-conventions):

```python
# backend/app/schemas/node.py (additions)
class EdgeCreate(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    label: str = "LINKS_TO"

    @field_validator("label")
    @classmethod
    def _label_allowed(cls, v: str) -> str:
        if v not in ALLOWED_EDGE_LABELS:   # from app.services.graph_service
            raise ValueError("unknown edge label")
        return v


class EdgeDelete(EdgeCreate):
    """Same shape as EdgeCreate: (source_id, target_id, label) identifies an edge."""


class EdgeOut(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    label: str
```

```python
# backend/app/api/v1/edges.py (as landed — see file for module docstring)
router = APIRouter(prefix="/edges", tags=["edges"])


@router.post("", response_model=EdgeOut, status_code=status.HTTP_201_CREATED,
             summary="Create edge", operation_id="createEdge")
async def create_edge(
    payload: EdgeCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> EdgeOut:
    # Both endpoints must be visible to the viewer (invisible == nonexistent).
    await ns.get_node(db, payload.source_id, viewer)
    await ns.get_node(db, payload.target_id, viewer)
    await gs.merge_edge(payload.source_id, payload.target_id, payload.label,
                        created_by=str(viewer.user_id))
    return EdgeOut(source_id=payload.source_id, target_id=payload.target_id, label=payload.label)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete edge", operation_id="deleteEdge")
async def delete_edge(
    payload: EdgeDelete,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> None:
    await ns.get_node(db, payload.source_id, viewer)
    await gs.delete_edge(payload.source_id, payload.target_id, payload.label)
```

- [x] **9.3** Create `graph.py` ([plan-fix]: an invisible center must be indistinguishable from a nonexistent one — `ns.get_node(db, node_id, viewer)` runs BEFORE the traversal and raises `NotFoundError` → 404 generic body (ADR-004); the plan's version would have returned a 200 neighborhood around an invisible center. Same `get_scoped_viewer`/`operation_id` fixes as 9.2):

```python
# backend/app/api/v1/graph.py (as landed)
router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("/neighborhood/{node_id}", response_model=GraphNeighborhoodOut,
            summary="Get node neighborhood", operation_id="getGraphNeighborhood")
async def get_neighborhood(
    node_id: uuid.UUID,
    hops: int = Query(1, ge=0, le=3),
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> GraphNeighborhoodOut:
    await ns.get_node(db, node_id, viewer)  # invisible center == nonexistent (404)
    data = await gs.get_neighborhood(db, node_id, viewer, hops=hops)
    return GraphNeighborhoodOut(**data)


@router.get("/overview", response_model=GraphNeighborhoodOut,
            summary="Get graph overview", operation_id="getGraphOverview")
async def get_overview(
    limit: int = Query(100, ge=1, le=500),
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> GraphNeighborhoodOut:
    data = await gs.get_overview(db, viewer, limit=limit)
    return GraphNeighborhoodOut(**data)
```

- [x] **9.4** Register routers in `main.py` ([plan-fix]: import style matches the existing `from app.api.v1.X import router as X_router` pattern) and add the Neo4j-down mapping to `core/errors.py`:

```python
# backend/app/main.py
from app.api.v1.edges import router as edges_router
from app.api.v1.graph import router as graph_router
app.include_router(edges_router, prefix="/api/v1")
app.include_router(graph_router, prefix="/api/v1")
```

```python
# backend/app/core/errors.py (addition inside register_error_handlers)
@app.exception_handler(ServiceUnavailable)          # neo4j.exceptions.ServiceUnavailable
async def neo4j_unavailable_handler(_: Request, exc: ServiceUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "graph backend unavailable"})
```

- [x] **9.5** Run tests:
```bash
cd backend && pytest tests/api/test_graph_api.py -v
# Observed GREEN (sandbox, Neo4j unreachable): 7 passed, 3 skipped (Neo4j skips:
# test_create_edge, test_delete_edge, test_neighborhood — re-verify on the Docker stack)
# Full suite: 68 passed, 9 skipped · ruff check clean ·
# mypy app/services app/schemas: Success: no issues found in 10 source files
```

- [x] **9.6** curl evidence ([plan-fix]: recorded honestly against a live uvicorn with Neo4j
  DOWN — edge writes and traversals are graph-sourced, so they surface 503
  `{"detail":"graph backend unavailable"}`; happy-path 201/200 for those endpoints requires
  the Docker stack and must be re-verified there. Evidence user/nodes deleted afterwards —
  `users` and `knowledge_nodes` counts are 0 again):

```
== POST /api/v1/edges (Neo4j down) ==          503  {"detail":"graph backend unavailable"}
== POST /api/v1/edges bad label ==             422  {"type":"value_error","loc":["body","label"],
                                                     "msg":"Value error, unknown edge label",...}
== DELETE /api/v1/edges (Neo4j down) ==        503  {"detail":"graph backend unavailable"}
== GET /graph/neighborhood/{id} (Neo4j down) = 503  {"detail":"graph backend unavailable"}
== GET /graph/neighborhood/<random uuid> ==    404  {"detail":"Node not found"}
== GET /graph/overview (nodes exist→Neo4j) ==  503  {"detail":"graph backend unavailable"}
# (pytest shows /graph/overview → 200 {"nodes":[],"edges":[]} when the viewer's visible
#  set is empty — the Neo4j edge lookup is skipped entirely in that case)
```

- [x] **9.7** Commit:
```
feat(api): POST /api/v1/edges, DELETE /api/v1/edges, GET /api/v1/graph/neighborhood, /overview
```

### Review fixes (Task 9 `/kb-review`)

- [x] **9.R1** ([plan-fix]: `app/core/deps.py` defined its own `Viewer` dataclass, structurally
  identical to `app.services.visibility.Viewer` — mypy on `app/api` failed with 12 arg-type
  errors at every service call site. deps now imports and re-exports the canonical Viewer
  (kb-conventions: Viewer is THE auth-context type). Verification gate widened:
  `mypy app/api app/services app/schemas` must pass — observed
  `Success: no issues found in 20 source files` with no strictness loosened.)

- [x] **9.R2** ([plan-fix]: `delete_edge` only vetted the SOURCE node's visibility; the target
  went unchecked, so a caller could probe/detach edges into another user's private nodes.
  DELETE now checks BOTH endpoints via `ns.get_node` before any Neo4j call, symmetric with
  `create_edge` (invisible == nonexistent → 404, ADR-004). RED→GREEN:
  `test_delete_edge_invisible_target_looks_not_found` — pure-PG, runs without Neo4j.)

---

## Task 10 — Daily logs API

**Files:**
- Create: `backend/app/api/v1/daily_logs.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_daily_logs_api.py`

### Steps

- [ ] **10.1** Write failing tests:

```python
# backend/tests/api/test_daily_logs_api.py
import pytest
from httpx import AsyncClient
from datetime import date

pytestmark = pytest.mark.asyncio


async def test_create_daily_log(client: AsyncClient, auth_headers):
    today = date.today().isoformat()
    r = await client.post(
        "/api/v1/daily-logs",
        json={"date": today, "body": "Today I worked on the KB system."},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert data["node_type"] == "daily_log"
    assert data["source"] == "daily_log"


async def test_get_daily_log_by_date(client: AsyncClient, auth_headers):
    today = date.today().isoformat()
    await client.post("/api/v1/daily-logs", json={"date": today, "body": "entry"}, headers=auth_headers)
    r = await client.get(f"/api/v1/daily-logs/{today}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["source_ref"] == today


async def test_daily_log_idempotent_create(client: AsyncClient, auth_headers):
    """Second POST for same date should upsert, not create duplicate."""
    today = date.today().isoformat()
    r1 = await client.post("/api/v1/daily-logs", json={"date": today, "body": "first"}, headers=auth_headers)
    r2 = await client.post("/api/v1/daily-logs", json={"date": today, "body": "second"}, headers=auth_headers)
    assert r1.json()["id"] == r2.json()["id"], "Same date must return same node ID"
    assert r2.json()["body"] == "second"
```

- [ ] **10.2** Create the router (daily logs are just KnowledgeNodes with node_type=daily_log):

```python
# backend/app/api/v1/daily_logs.py
from __future__ import annotations

from datetime import date
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_viewer
from app.models.knowledge import KnowledgeNode, NodeType
from app.models.user import Visibility
from app.schemas.node import NodeOut
from app.services import node_service as ns
from app.services.visibility import Viewer, visible_nodes_clause

router = APIRouter(prefix="/daily-logs", tags=["daily_logs"])


class DailyLogCreate(BaseModel):
    date: date
    body: str = ""


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def upsert_daily_log(
    payload: DailyLogCreate,
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    date_str = payload.date.isoformat()
    # Check if log already exists for this user+date
    existing = await db.scalar(
        select(KnowledgeNode).where(
            KnowledgeNode.owner_id == viewer.user_id,
            KnowledgeNode.node_type == NodeType.daily_log.value,
            KnowledgeNode.source == "daily_log",
            KnowledgeNode.source_ref == date_str,
            KnowledgeNode.deleted_at.is_(None),
        )
    )
    if existing:
        node = await ns.update_node(db, existing.id, viewer, body=payload.body)
    else:
        node = await ns.create_node(
            db,
            viewer=viewer,
            title=f"Daily Log — {date_str}",
            body=payload.body,
            node_type=NodeType.daily_log.value,
            visibility=Visibility.private,
            source="daily_log",
            source_ref=date_str,
        )
    await db.commit()
    return node


@router.get("/{log_date}", response_model=NodeOut)
async def get_daily_log(
    log_date: date,
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    from app.core.errors import NotFoundError
    date_str = log_date.isoformat()
    clause = visible_nodes_clause(viewer)
    node = await db.scalar(
        select(KnowledgeNode).where(
            clause,
            KnowledgeNode.node_type == NodeType.daily_log.value,
            KnowledgeNode.source_ref == date_str,
        )
    )
    if node is None:
        raise NotFoundError(f"No daily log for {date_str}")
    return node


@router.get("", response_model=list[NodeOut])
async def list_daily_logs(
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    clause = visible_nodes_clause(viewer)
    rows = await db.scalars(
        select(KnowledgeNode).where(
            clause,
            KnowledgeNode.node_type == NodeType.daily_log.value,
        ).order_by(KnowledgeNode.source_ref.desc()).limit(90)  # last 90 days
    )
    return list(rows)
```

- [ ] **10.3** Register in `main.py`:

```python
from app.api.v1 import daily_logs as daily_logs_router
app.include_router(daily_logs_router.router, prefix="/api/v1")
```

- [ ] **10.4** Run all tests:
```bash
cd backend && pytest tests/ -v
# Expected: all pass, no skips
```

- [ ] **10.5** Run full lint + type check:
```bash
cd backend && ruff check . && mypy --strict app/services/ app/schemas/
```

- [ ] **10.6** curl evidence:
```bash
curl -s -X POST http://localhost:8000/api/v1/daily-logs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"date\": \"$(date +%F)\", \"body\": \"Worked on phase 1 today.\"}" | jq .node_type
# Expected: "daily_log"
```

- [ ] **10.7** Commit:
```
feat(api): POST/GET /api/v1/daily-logs with upsert-by-date semantics
```

---

## Phase 1 exit gate

Run `/kb-verify` and confirm:

```bash
cd backend
pytest tests/ -v --tb=short              # all green
ruff check .                              # no errors
mypy --strict app/services/ app/schemas/ # no errors

# Visibility audit (must return 0 matches):
grep -rn "select.*knowledge_nodes\|from knowledge_nodes\|KnowledgeNode\)" \
  app/api/ app/services/ | grep -v visibility.py | grep -v node_service.py \
  | grep -v graph_service.py | grep -v daily_logs.py
# Expected: 0 lines (every raw query goes through visibility.py)

alembic upgrade head  # clean
```

Update `docs/plans/README.md` — change Phase 1 Status from `Not started` to `Done`.
