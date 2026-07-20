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
- Update: `backend/app/services/graph_service.py` (full implementation — driver setup skeleton was in Task 3)
- Create: `backend/tests/services/test_graph_service.py`

### Steps

- [ ] **5.1** Write the failing tests:

```python
# backend/tests/services/test_graph_service.py
import uuid
import pytest
from app.models.knowledge import KnowledgeNode
from app.models.user import Visibility, Role
from app.services.visibility import Viewer
from app.services import graph_service as gs

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


async def test_neighborhood_visibility(db, make_user, make_node):
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
    assert str(private_node.id) not in node_ids, "Private node must not leak through graph traversal"
```

- [ ] **5.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_graph_service.py -x 2>&1 | head -20
```

- [ ] **5.3** Implement `graph_service.py`:

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
) -> dict[str, list[dict]]:
    """
    Return nodes and edges within `hops` hops of center_id, visibility-filtered.
    hops clamped to _HOP_LIMIT.  Total nodes capped at _NODE_LIMIT.
    Visibility is enforced by re-querying PG (the authoritative source).
    """
    hops = min(hops, _HOP_LIMIT)
    candidate_ids: set[uuid.UUID] = {center_id}
    raw_edges: list[dict] = []

    async with get_driver().session() as session:
        result = await session.run(
            f"""
            MATCH (center:Node {{node_id: $cid}})-[e*0..{hops}]-(other:Node)
            WHERE other.deleted IS NULL OR other.deleted = false
            WITH collect(DISTINCT other) AS nodes,
                 collect(DISTINCT e)    AS edge_lists
            RETURN nodes, edge_lists
            LIMIT $limit
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
                        "target": e.end_node["node_id"]   if hasattr(e, "end_node")   else None,
                        "label":  e.type                  if hasattr(e, "type")        else "",
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

    nodes_out = [
        {"id": str(n.id), "title": n.title, "node_type": n.node_type, "visibility": n.visibility.value}
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
) -> dict[str, list[dict]]:
    """Top visible nodes + edges between them for the initial graph viewport."""
    clause = visible_nodes_clause(viewer)
    rows = await db.scalars(
        select(KnowledgeNode).where(clause).order_by(KnowledgeNode.updated_at.desc()).limit(limit)
    )
    nodes = list(rows)
    id_set = {str(n.id) for n in nodes}

    nodes_out = [{"id": str(n.id), "title": n.title, "node_type": n.node_type} for n in nodes]

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

- [ ] **5.4** Run tests:
```bash
cd backend && pytest tests/services/test_graph_service.py -v
# Expected: 3 passed
```

- [ ] **5.5** Commit:
```
feat(graph): graph_service with Neo4j driver — upsert_vertex, merge/delete edge, neighborhood + visibility gate
```

---

## Task 6 — Node service (CRUD + wikilinks + visibility change)

**Files:**
- Create: `backend/app/services/node_service.py`
- Create: `backend/tests/services/test_node_service.py`

### Steps

- [ ] **6.1** Write the failing tests:

```python
# backend/tests/services/test_node_service.py
import uuid
import pytest
from app.models.user import Visibility, Role
from app.services.visibility import Viewer
from app.services import node_service as ns

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


async def test_wikilink_extraction(db, make_user, make_node):
    owner = await make_user(email="ns_wl@test.com")
    n1 = await make_node(owner, title="Source Note", body="see [[Target Note]] and [[Other]]")
    n2 = await make_node(owner, title="Target Note", body="")
    await db.flush()
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await ns.resolve_wikilinks(db, n1, viewer)
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

- [ ] **6.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_node_service.py -x 2>&1 | head -20
```

- [ ] **6.3** Implement `node_service.py`:

```python
# backend/app/services/node_service.py
from __future__ import annotations

import re
import uuid
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.models.knowledge import KnowledgeNode, NodeRevision, NodeType
from app.models.user import Role, Visibility
from app.services import graph_service as gs
from app.services.visibility import Viewer, visible_nodes_clause

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


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
    # NOTE: Neo4j vertex upsert happens AFTER db.commit() in the calling code path.
    # node_service.create_node() callers must call gs.upsert_vertex(node) post-commit,
    # or the router does so after awaiting the service (see kb-neo4j-graph skill).
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
    from sqlalchemy import func
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

    # Save revision before mutating
    rev_count = await db.scalar(
        select(func.count()).select_from(NodeRevision).where(NodeRevision.node_id == node_id)
    ) or 0
    revision = NodeRevision(
        id=uuid.uuid4(),
        node_id=node.id,
        version=rev_count + 1,
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
    await db.flush()
    await gs.create_vertex(db, node)  # sync vertex props
    return node


async def delete_node(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> None:
    node = await get_node(db, node_id, viewer)
    if node.owner_id != viewer.user_id and viewer.role != Role.admin:
        raise ForbiddenError("Only owner or admin can delete a node")
    node.deleted_at = datetime.now(UTC)
    await db.flush()
    await gs.soft_delete_vertex(db, node_id)


async def resolve_wikilinks(db: AsyncSession, node: KnowledgeNode, viewer: Viewer) -> None:
    """
    Find [[Title]] references in node.body, resolve to node IDs by title,
    and MERGE LINKS_TO edges in Neo4j via graph_service.
    Unresolved titles are silently skipped.
    """
    titles = _WIKILINK_RE.findall(node.body)
    if not titles:
        return

    # Ensure source vertex exists
    await gs.create_vertex(db, node)

    clause = visible_nodes_clause(viewer)
    for title in set(titles):
        target = await db.scalar(
            select(KnowledgeNode)
            .where(KnowledgeNode.title == title)
            .where(clause)
            .limit(1)
        )
        if target is None:
            continue
        await gs.create_vertex(db, target)
        await gs.merge_edge(db, node.id, target.id, "LINKS_TO", props={"created_by": "wikilink"})


# Fix missing import
from sqlalchemy import func  # noqa: E402 (moved here to avoid circular; OK in service layer)
```

- [ ] **6.4** Run tests:
```bash
cd backend && pytest tests/services/test_node_service.py -v
# Expected: 5 passed
```

- [ ] **6.5** Commit:
```
feat(node_service): CRUD, revisions, wikilink resolution, soft-delete
```

---

## Task 7 — Pydantic schemas for nodes

**Files:**
- Create: `backend/app/schemas/node.py`
- Create: `backend/tests/schemas/test_node_schemas.py`

### Steps

- [ ] **7.1** Write the failing test:

```python
# backend/tests/schemas/test_node_schemas.py
import uuid
from datetime import datetime, UTC
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
        deleted_at=None,
    )
    out = NodeOut(**data)
    dumped = out.model_dump()
    assert "password_hash" not in dumped
    assert "body_tsv" not in dumped


def test_node_update_partial():
    u = NodeUpdate(title="New")
    assert u.body is None
    assert u.title == "New"
```

- [ ] **7.2** Run — expect ImportError:
```bash
cd backend && pytest tests/schemas/test_node_schemas.py -x 2>&1 | head -10
```

- [ ] **7.3** Create schemas:

```python
# backend/app/schemas/node.py
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

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
    deleted_at: datetime | None

    model_config = {"from_attributes": True}


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

- [ ] **7.4** Run tests:
```bash
cd backend && pytest tests/schemas/test_node_schemas.py -v
# Expected: 3 passed
```

- [ ] **7.5** Commit:
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

**Files:**
- Create: `backend/app/api/v1/nodes.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_nodes_api.py`

### Steps

- [ ] **8.1** Write the failing tests:

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


async def test_get_private_node_other_user_forbidden(client: AsyncClient, auth_headers, auth_headers_other):
    r = await client.post("/api/v1/nodes", json={"title": "Private", "visibility": "private"}, headers=auth_headers)
    node_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/nodes/{node_id}", headers=auth_headers_other)
    assert r2.status_code == 403


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

- [ ] **8.2** Run — expect 404 (router not registered):
```bash
cd backend && pytest tests/api/test_nodes_api.py -x 2>&1 | head -20
```

- [ ] **8.3** Create the router:

```python
# backend/app/api/v1/nodes.py
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_viewer, Pagination
from app.core.db import get_db
from app.schemas.node import NodeCreate, NodeListOut, NodeOut, NodeUpdate
from app.services import node_service as ns

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(
    payload: NodeCreate,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    node = await ns.create_node(
        db,
        viewer=viewer,
        title=payload.title,
        body=payload.body,
        node_type=payload.node_type,
        visibility=payload.visibility,
        source=payload.source,
        source_ref=payload.source_ref,
        meta=payload.meta,
    )
    await db.commit()
    return node


@router.get("", response_model=NodeListOut)
async def list_nodes(
    pagination: Pagination = Depends(),
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    items, total = await ns.list_nodes(db, viewer, offset=pagination.offset, limit=pagination.limit)
    return NodeListOut(items=items, total=total, offset=pagination.offset, limit=pagination.limit)


@router.get("/{node_id}", response_model=NodeOut)
async def get_node(
    node_id: uuid.UUID,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    return await ns.get_node(db, node_id, viewer)


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: uuid.UUID,
    payload: NodeUpdate,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    node = await ns.update_node(
        db, node_id, viewer,
        title=payload.title,
        body=payload.body,
        visibility=payload.visibility,
        meta=payload.meta,
    )
    await db.commit()
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    node_id: uuid.UUID,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    await ns.delete_node(db, node_id, viewer)
    await db.commit()


@router.post("/{node_id}/shares", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def share_node(
    node_id: uuid.UUID,
    payload: "NodeShareCreate",
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    from app.schemas.node import NodeShareCreate as NSC
    from app.models.knowledge import NodeShare
    node = await ns.get_node(db, node_id, viewer)
    share = NodeShare(node_id=node.id, user_id=payload.user_id, group_id=payload.group_id, can_edit=payload.can_edit)
    db.add(share)
    await db.commit()
    await db.refresh(node)
    return node
```

- [ ] **8.4** Register router in `main.py`:

```python
# backend/app/main.py  (add inside create_app, after existing routers)
from app.api.v1 import nodes as nodes_router
app.include_router(nodes_router.router, prefix="/api/v1")
```

- [ ] **8.5** Add `auth_headers_other` fixture and `Pagination` dep if not present:

```python
# backend/tests/conftest.py  (add auth_headers_other fixture)
@pytest_asyncio.fixture
async def auth_headers_other(client):
    await client.post("/api/v1/auth/register", json={
        "email": "other@test.com", "password": "pass1234", "display_name": "Other"
    })
    r = await client.post("/api/v1/auth/login", data={"username": "other@test.com", "password": "pass1234"})
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
```

```python
# backend/app/core/deps.py  (add Pagination)
from fastapi import Query

class Pagination:
    def __init__(self, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        self.offset = offset
        self.limit = limit
```

- [ ] **8.6** Run tests:
```bash
cd backend && pytest tests/api/test_nodes_api.py -v
# Expected: 6 passed
```

- [ ] **8.7** curl evidence:
```bash
# Obtain token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=admin@kb.local&password=admin1234" | jq -r .access_token)

# Create node
curl -s -X POST http://localhost:8000/api/v1/nodes \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"First Node","body":"hello [[Second Node]]","visibility":"public"}' | jq .

# List nodes
curl -s http://localhost:8000/api/v1/nodes \
  -H "Authorization: Bearer $TOKEN" | jq .total
```

- [ ] **8.8** Commit:
```
feat(api): POST/GET/PATCH/DELETE /api/v1/nodes with visibility enforcement
```

---

## Task 9 — Graph & edges API routers

**Files:**
- Create: `backend/app/api/v1/edges.py`
- Create: `backend/app/api/v1/graph.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_graph_api.py`

### Steps

- [ ] **9.1** Write failing tests:

```python
# backend/tests/api/test_graph_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_create_edge(client: AsyncClient, auth_headers):
    r1 = await client.post("/api/v1/nodes", json={"title": "N1", "visibility": "public"}, headers=auth_headers)
    r2 = await client.post("/api/v1/nodes", json={"title": "N2", "visibility": "public"}, headers=auth_headers)
    n1_id, n2_id = r1.json()["id"], r2.json()["id"]
    r = await client.post("/api/v1/edges", json={"source_id": n1_id, "target_id": n2_id, "label": "LINKS_TO"}, headers=auth_headers)
    assert r.status_code == 201


async def test_neighborhood(client: AsyncClient, auth_headers):
    r1 = await client.post("/api/v1/nodes", json={"title": "Center", "visibility": "public"}, headers=auth_headers)
    r2 = await client.post("/api/v1/nodes", json={"title": "Neighbour", "visibility": "public"}, headers=auth_headers)
    n1_id, n2_id = r1.json()["id"], r2.json()["id"]
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
```

- [ ] **9.2** Create `edges.py`:

```python
# backend/app/api/v1/edges.py
import uuid
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.deps import get_current_viewer
from app.core.db import get_db
from app.services import graph_service as gs
from app.services import node_service as ns

router = APIRouter(prefix="/edges", tags=["edges"])


class EdgeCreate(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    label: str = "LINKS_TO"
    props: dict = {}


class EdgeDelete(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    label: str = "LINKS_TO"


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_edge(
    payload: EdgeCreate,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    # Verify both nodes are visible to viewer
    await ns.get_node(db, payload.source_id, viewer)
    await ns.get_node(db, payload.target_id, viewer)
    await gs.merge_edge(db, payload.source_id, payload.target_id, payload.label, props=payload.props)
    await db.commit()
    return {"source_id": str(payload.source_id), "target_id": str(payload.target_id), "label": payload.label}


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edge(
    payload: EdgeDelete,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    await ns.get_node(db, payload.source_id, viewer)
    await gs.delete_edge(db, payload.source_id, payload.target_id, payload.label)
    await db.commit()
```

- [ ] **9.3** Create `graph.py`:

```python
# backend/app/api/v1/graph.py
import uuid
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.deps import get_current_viewer
from app.core.db import get_db
from app.schemas.node import GraphNeighborhoodOut
from app.services import graph_service as gs

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("/neighborhood/{node_id}", response_model=GraphNeighborhoodOut)
async def get_neighborhood(
    node_id: uuid.UUID,
    hops: int = Query(1, ge=0, le=3),
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    return await gs.get_neighborhood(db, node_id, viewer, hops=hops)


@router.get("/overview", response_model=GraphNeighborhoodOut)
async def get_overview(
    limit: int = Query(100, ge=1, le=500),
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    return await gs.get_overview(db, viewer, limit=limit)
```

- [ ] **9.4** Register routers in `main.py`:

```python
from app.api.v1 import edges as edges_router, graph as graph_router
app.include_router(edges_router.router, prefix="/api/v1")
app.include_router(graph_router.router, prefix="/api/v1")
```

- [ ] **9.5** Run tests:
```bash
cd backend && pytest tests/api/test_graph_api.py -v
# Expected: 3 passed
```

- [ ] **9.6** Commit:
```
feat(api): POST /api/v1/edges, DELETE /api/v1/edges, GET /api/v1/graph/neighborhood, /overview
```

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
