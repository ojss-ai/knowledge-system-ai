# Phase 4 — Bulk Markdown Import

**Goal:** Let users POST a zip/directory of Markdown files; the system parses them, creates KnowledgeNodes, resolves `[[wikilinks]]` in a two-pass algorithm, and streams real-time progress over WebSocket. Service tokens (for CLI tools) are also created here.

**Architecture refs:** ADR-001, ADR-007 (Celery), ADR-004 (visibility)

**Required skills (read before any task):**
- `kb-conventions`
- `kb-tdd-workflow`
- `kb-visibility-filter`
- `kb-celery-jobs`
- `kb-ingestion-connectors` — KnowledgeIngestor contract, two-pass edge resolution, IngestItem
- `kb-api-conventions`

**Exit criteria:**
- [ ] All tasks checked
- [ ] `pytest -x backend/tests/` green
- [ ] `ruff check backend/` clean
- [ ] Idempotency test for MD ingestor passes (ingest twice → same node count)
- [ ] WebSocket progress events confirmed via `wscat` or integration test
- [ ] curl evidence for `POST /api/v1/uploads/markdown`

---

## Task 1 — ingestion_runs + api_tokens models + MinIO client

> [plan-fix] Title said "attachments + ingestion_runs models" but no task in this
> plan defines an `attachments` table/model — retitled to match the actual steps
> (IngestionRun + ApiToken). The `attachments` table lands with AttachmentSpec
> handling in a later phase.

**Files:**
- Create: `backend/app/models/ingest.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0005_ingest.py`
- Create: `backend/app/services/storage.py`
- Create: `backend/tests/models/test_ingest_models.py`
- Create: `backend/tests/services/test_storage.py` ([plan-fix] TDD coverage for ApiToken + storage)

### Steps

- [x] **1.1** Write the failing test:

```python
# backend/tests/models/test_ingest_models.py
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
```

- [x] **1.2** Create the model ([plan-fix] `RunStatus` is `enum.StrEnum`, not
  `(str, enum.Enum)` — matches Role/Visibility/NodeType across the codebase):

```python
# backend/app/models/ingest.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RunStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # md_upload|confluence|codebase
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), nullable=False, default=RunStatus.pending
    )
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiToken(Base):
    """Service tokens for CLI tools (Confluence sync, codebase scanner)."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)  # ["ingest"]
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(nullable=False, default=False)
```

- [x] **1.3** Add to `backend/app/models/__init__.py` ([plan-fix] sorted imports +
  `__all__` entries instead of a `# noqa: F401` line — matches the file's style):
```python
from app.models.ingest import ApiToken, IngestionRun, RunStatus
# ...and "ApiToken", "IngestionRun", "RunStatus" added to __all__
```

- [x] **1.4** Write migration `0005_ingest.py` ([plan-fix] plan's block created the
  `run_status` enum explicitly AND reused the same `sa.Enum` inside `create_table`,
  which emits CREATE TYPE twice → DuplicateObject. Uses the 0002 pattern
  (`postgresql.ENUM(..., create_type=False)`); index names follow the model's
  `index=True` convention (`ix_..._owner_id`); counters get `server_default` since
  `default=` is a no-op in DDL):

```python
# backend/alembic/versions/0005_ingest.py
"""ingestion_runs and api_tokens

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

run_status_enum = postgresql.ENUM(
    "pending", "running", "done", "failed", name="run_status", create_type=False
)


def upgrade() -> None:
    run_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", run_status_enum, nullable=False, server_default="pending"),
        sa.Column("total_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("processed_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("failed_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_log", sa.Text),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index(op.f("ix_ingestion_runs_owner_id"), "ingestion_runs", ["owner_id"])

    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("scopes", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index(op.f("ix_api_tokens_owner_id"), "api_tokens", ["owner_id"])


def downgrade() -> None:
    op.drop_table("api_tokens")
    op.drop_table("ingestion_runs")
    run_status_enum.drop(op.get_bind(), checkfirst=True)
```

- [x] **1.5** Create MinIO storage client ([plan-fix] mypy --strict scope covers
  `app/services`: `_client()` gets a `-> Minio` annotation with a module-level
  typed import (minio 7.2 ships py.typed); dropped the unused `import io`.
  Behaviour is covered by `tests/services/test_storage.py` with a faked Minio
  client — MinIO server is a true boundary; live round-trip is verified against
  the Docker stack. `minio>=7.2` added to pyproject dependencies):

```python
# backend/app/services/storage.py
"""MinIO / S3-compatible object storage client.

Used for storing original uploaded files.
"""

from __future__ import annotations

from typing import BinaryIO

from minio import Minio

from app.core.config import settings


def _client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def ensure_bucket(bucket: str = "kb-uploads") -> None:
    client = _client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def upload_file(
    object_name: str,
    data: BinaryIO,
    length: int,
    content_type: str = "application/octet-stream",
    bucket: str = "kb-uploads",
) -> str:
    """Upload to MinIO and return the object path."""
    ensure_bucket(bucket)
    client = _client()
    client.put_object(bucket, object_name, data, length, content_type=content_type)
    return f"{bucket}/{object_name}"


def download_file(object_path: str) -> bytes:
    """Download from MinIO. object_path = 'bucket/key'."""
    bucket, key = object_path.split("/", 1)
    client = _client()
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
```

- [x] **1.6** Add MinIO settings to `config.py`:

```python
# backend/app/core/config.py  (add)
minio_endpoint: str = "localhost:9000"
minio_access_key: str = "minioadmin"
minio_secret_key: str = "minioadmin"
minio_secure: bool = False
```

- [x] **1.7** Apply migration and run tests:
```bash
cd backend && alembic upgrade head
pytest tests/models/test_ingest_models.py tests/services/test_storage.py -v
# Expected: 5 passed (2 model + 3 storage)
```

- [x] **1.8** Commit:
```
feat(models): ingestion_runs, api_tokens + migration 0005; MinIO storage client
```

---

## Task 2 — KnowledgeIngestor + IngestItem contract

**Files:**
- Create: `backend/app/services/ingest/__init__.py`
- Create: `backend/app/services/ingest/base.py`
- Create: `backend/tests/services/ingest/__init__.py` ([plan-fix] test dirs are packages in this repo)
- Create: `backend/tests/services/ingest/test_ingest_base.py`
- Modify: `backend/app/services/node_service.py` ([plan-fix] `_queue_graph_op` promoted to public
  `queue_graph_op` — the ingestor queues its graph MERGEs through the same session mechanism)

### Steps

- [x] **2.1** Write the failing test ([plan-fix] `test_two_pass_edge_resolution` verifies edges
  via live Neo4j, so it gets the `neo4j_session` skip fixture and runs
  `ns.run_pending_graph_ops(db)` — the ingestor QUEUES graph ops (PG-first, ADR-011), it never
  awaits Neo4j in-transaction. Recorder-based tests added (pattern from
  `tests/services/test_node_service.py`) so two-pass ordering and dangling-ref handling stay
  verified without a live Neo4j — kb-ingestion-connectors lists dangling-link handling as a
  mandatory test):

```python
# backend/tests/services/ingest/test_ingest_base.py
import pytest

from app.models.user import Role
from app.services import node_service as ns
from app.services.ingest.base import EdgeSpec, IngestItem, KnowledgeIngestor
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_upsert_new_node(db, make_user):
    owner = await make_user(email="ing_base@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    item = IngestItem(
        source="md_upload",
        source_ref="test/note.md",
        title="Test Note",
        body="# Hello\n\nThis is a test.",
        node_type="note",
        tags=["test"],
    )
    node = await ingestor.upsert(item)
    assert node.id is not None
    assert node.source == "md_upload"
    assert node.source_ref == "test/note.md"


async def test_upsert_idempotent(db, make_user):
    """Upserting the same source_ref twice returns the same node ID."""
    owner = await make_user(email="ing_idem@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    item = IngestItem(
        source="md_upload", source_ref="idem/test.md", title="Idempotent", body="body"
    )
    n1 = await ingestor.upsert(item)
    n2 = await ingestor.upsert(item)
    assert n1.id == n2.id


async def test_two_pass_edge_resolution(db, neo4j_session, make_user):
    """Edges with forward references are resolved after all nodes are ingested."""
    owner = await make_user(email="ing_edge@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    n1 = await ingestor.upsert(
        IngestItem(source="md", source_ref="a.md", title="A", body="see [[B]]")
    )
    n2 = await ingestor.upsert(
        IngestItem(source="md", source_ref="b.md", title="B", body="content")
    )

    ingestor.add_edge_spec(EdgeSpec(source_ref="a.md", target_ref="b.md", label="LINKS_TO"))
    await ingestor.resolve_edges()
    # The ingestor only QUEUES graph ops (PG-first, ADR-011); run them as the
    # post-commit caller would.
    await ns.run_pending_graph_ops(db)

    from app.services import graph_service as gs

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    targets = [e["target"] for e in hood["edges"]]
    assert str(n2.id) in targets


# --- PG-first invariant + dangling refs, verified via recorder (no live Neo4j) ---


def _graph_recorder(monkeypatch):
    """Patch graph_service functions with recorders; return the call log."""
    from app.services import graph_service as gs

    calls: list[tuple[str, ...]] = []

    async def fake_upsert(node):
        calls.append(("upsert", str(node.id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        calls.append(("edge", str(source_id), str(target_id), label, created_by))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)
    return calls


async def test_resolve_edges_defers_graph_sync(db, make_user, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_defer@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    n1 = await ingestor.upsert(IngestItem(source="md", source_ref="x.md", title="X", body="x"))
    n2 = await ingestor.upsert(IngestItem(source="md", source_ref="y.md", title="Y", body="y"))
    ingestor.add_edge_spec(EdgeSpec(source_ref="x.md", target_ref="y.md", label="LINKS_TO"))
    await ingestor.resolve_edges()

    assert calls == []  # nothing hit Neo4j inside the transaction
    await ns.run_pending_graph_ops(db)
    assert ("edge", str(n1.id), str(n2.id), "LINKS_TO", "ingest") in calls


async def test_resolve_edges_skips_dangling_refs(db, make_user, monkeypatch):
    """Unresolvable refs are skipped silently, never raised (kb-ingestion-connectors)."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="ing_dangle@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    ingestor = KnowledgeIngestor(db, viewer)

    await ingestor.upsert(IngestItem(source="md", source_ref="only.md", title="Only", body="o"))
    ingestor.add_edge_spec(
        EdgeSpec(source_ref="only.md", target_ref="missing.md", label="LINKS_TO")
    )
    await ingestor.resolve_edges()
    await ns.run_pending_graph_ops(db)

    assert all(c[0] != "edge" for c in calls)
```

- [x] **2.2** Create `base.py` ([plan-fix] plan block drifted from the codebase:
  `gs.create_vertex` does not exist → `gs.upsert_vertex(node)` (no db arg);
  `gs.merge_edge(source_id, target_id, label, created_by, score)` takes no db arg and no
  `props=` kwarg → `created_by`/`score` are pulled from `EdgeSpec.props`; `resolve_edges`
  awaited Neo4j inside the transaction → it now QUEUES ops via `ns.queue_graph_op`
  (PG-first, ADR-011), caller runs `run_pending_graph_ops(db)` post-commit; the
  existing-node probe selected `knowledge_nodes` without `visible_nodes_clause`
  (kb-visibility-filter rule 1) and without owner scoping — the idempotency key is
  per-owner (`uq_node_owner_source_ref`), so the probe pins `owner_id == viewer.user_id`;
  `import uuid` hoisted to module level; mypy --strict scope annotations):

```python
# backend/app/services/ingest/base.py
"""KnowledgeIngestor — the single convergence point for all ingestion paths
(kb-ingestion-connectors): connectors fetch + convert into IngestItems; this
module owns persistence. Never write connector-specific node persistence.

PG first, Neo4j second (ADR-011): upsert() persists rows via node_service
(which queues the vertex sync); resolve_edges() only QUEUES edge MERGEs on the
session via node_service.queue_graph_op. The caller commits, then runs
node_service.run_pending_graph_ops(db).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode, NodeTag, NodeType, Tag
from app.models.user import Visibility
from app.services import graph_service as gs
from app.services import node_service as ns
from app.services.visibility import Viewer, visible_nodes_clause


@dataclass
class IngestItem:
    source: str  # "md_upload" | "confluence" | "codebase"
    source_ref: str  # unique external key (path, page_id, symbol_fqn)
    title: str
    body: str
    node_type: str = NodeType.note.value
    visibility: Visibility = Visibility.private
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256((self.title + self.body).encode()).hexdigest()


@dataclass
class EdgeSpec:
    source_ref: str  # source_ref of the source node
    target_ref: str  # source_ref of the target node
    label: str = "LINKS_TO"
    props: dict[str, Any] = field(default_factory=dict)


class KnowledgeIngestor:
    """
    Single convergence point for all ingestion paths.

    Usage:
        ingestor = KnowledgeIngestor(db, viewer)
        for item in items:
            await ingestor.upsert(item)
            ingestor.add_edge_spec(EdgeSpec(...))  # collect during pass 1
        await ingestor.resolve_edges()             # queue edges in pass 2
        # caller: await db.commit(); await node_service.run_pending_graph_ops(db)
    """

    def __init__(self, db: AsyncSession, viewer: Viewer) -> None:
        self._db = db
        self._viewer = viewer
        self._ref_to_node: dict[str, KnowledgeNode] = {}
        self._edge_specs: list[EdgeSpec] = []

    async def upsert(self, item: IngestItem) -> KnowledgeNode:
        """
        Create or update a KnowledgeNode for the given IngestItem.
        Idempotent: same (source, source_ref) → same node ID.
        Content-hash short-circuit: unchanged content → skip body update.
        """
        # visible_nodes_clause is mandatory on every knowledge_nodes read
        # (kb-visibility-filter rule 1). The extra owner_id predicate pins the
        # probe to the idempotency key (owner_id, source, source_ref) — the
        # clause alone would also match ANOTHER owner's public node with the
        # same source_ref, and updating that must never happen here.
        existing = await self._db.scalar(
            select(KnowledgeNode).where(
                visible_nodes_clause(self._viewer),
                KnowledgeNode.owner_id == self._viewer.user_id,
                KnowledgeNode.source == item.source,
                KnowledgeNode.source_ref == item.source_ref,
            )
        )

        if existing is not None:
            existing_hash = hashlib.sha256((existing.title + existing.body).encode()).hexdigest()
            if existing_hash != item.content_hash:
                # Content changed — update
                existing = await ns.update_node(
                    self._db,
                    existing.id,
                    self._viewer,
                    title=item.title,
                    body=item.body,
                    meta={**item.meta, "_content_hash": item.content_hash},
                )
            node = existing
        else:
            node = await ns.create_node(
                self._db,
                viewer=self._viewer,
                title=item.title,
                body=item.body,
                node_type=item.node_type,
                visibility=item.visibility,
                source=item.source,
                source_ref=item.source_ref,
                meta={**item.meta, "_content_hash": item.content_hash},
            )

        # Tags
        for tag_name in item.tags:
            slug = tag_name.lower().replace(" ", "-")
            tag = await self._db.scalar(select(Tag).where(Tag.slug == slug))
            if tag is None:
                tag = Tag(id=uuid.uuid4(), name=tag_name, slug=slug)
                self._db.add(tag)
                await self._db.flush()
            # Idempotent node_tag association
            existing_nt = await self._db.scalar(
                select(NodeTag).where(NodeTag.node_id == node.id, NodeTag.tag_id == tag.id)
            )
            if existing_nt is None:
                self._db.add(NodeTag(node_id=node.id, tag_id=tag.id))
                await self._db.flush()

        self._ref_to_node[item.source_ref] = node
        return node

    def add_edge_spec(self, spec: EdgeSpec) -> None:
        """Queue an edge for resolution in pass 2."""
        self._edge_specs.append(spec)

    async def resolve_edges(self) -> None:
        """
        Pass 2: resolve queued EdgeSpecs to node IDs and QUEUE the graph MERGEs
        for post-commit run_pending_graph_ops() — never awaited in-transaction
        (ADR-011). Unresolvable refs are silently skipped (dangling links are
        expected in batch imports, not errors).
        """
        for spec in self._edge_specs:
            src_node = self._ref_to_node.get(spec.source_ref)
            tgt_node = self._ref_to_node.get(spec.target_ref)
            if src_node is None or tgt_node is None:
                continue
            score = spec.props.get("score")
            ns.queue_graph_op(self._db, partial(gs.upsert_vertex, src_node))
            ns.queue_graph_op(self._db, partial(gs.upsert_vertex, tgt_node))
            ns.queue_graph_op(
                self._db,
                partial(
                    gs.merge_edge,
                    src_node.id,
                    tgt_node.id,
                    spec.label,
                    created_by=str(spec.props.get("created_by", "ingest")),
                    score=float(score) if score is not None else None,
                ),
            )

        self._edge_specs.clear()
```

- [x] **2.3** Run tests:
```bash
cd backend && pytest tests/services/ingest/test_ingest_base.py -v
# Expected: 5 passed with Neo4j up; 4 passed + 1 skipped when Neo4j is unreachable
# ([plan-fix] count was 3; two recorder-based tests added, live-graph test skips without Neo4j)
```

- [x] **2.4** Commit:
```
feat(ingest): KnowledgeIngestor with IngestItem contract and two-pass edge resolution
```

---

## Task 3 — Markdown importer

**Files:**
- Create: `backend/app/services/ingest/md_importer.py`
- Create: `backend/tests/services/ingest/test_md_importer.py`

### Steps

- [x] **3.1** Write failing tests:

> [plan-fix] Plan block set `pytestmark = pytest.mark.asyncio` on an all-sync
> module (pure parsing, no DB) — pytest-asyncio emits PytestWarnings for that,
> violating "output pristine". Mark dropped. The plan also imported IngestItem
> without using it (ruff F401); it is now exercised via an isinstance assertion.

```python
# backend/tests/services/ingest/test_md_importer.py
import io
import zipfile

from app.services.ingest.base import IngestItem
from app.services.ingest.md_importer import extract_wikilinks, parse_zip

# [plan-fix] plan set `pytestmark = pytest.mark.asyncio`, but every test here is
# synchronous (pure parsing, no DB) — the mark only produced PytestWarnings.


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_parse_zip_returns_ingest_items():
    zip_bytes = make_zip(
        {
            "notes/hello.md": "# Hello World\n\nThis is a note.",
            "notes/bye.md": "# Goodbye\n\nSee [[Hello World]] for details.",
        }
    )
    items, edge_specs = parse_zip(zip_bytes, source="md_upload")
    assert len(items) == 2
    assert all(isinstance(i, IngestItem) for i in items)
    titles = [i.title for i in items]
    assert "Hello World" in titles
    assert "Goodbye" in titles


def test_parse_zip_extracts_wikilink_edge_specs():
    zip_bytes = make_zip(
        {
            "a.md": "# A\n\nLinks to [[B]].",
            "b.md": "# B\n\nContent.",
        }
    )
    items, edge_specs = parse_zip(zip_bytes, source="md_upload")
    assert any(e.source_ref.endswith("a.md") and e.target_ref.endswith("b.md") for e in edge_specs)


def test_extract_wikilinks():
    links = extract_wikilinks("See [[Alpha]] and [[Beta]].")
    assert links == ["Alpha", "Beta"]


def test_non_md_files_skipped():
    zip_bytes = make_zip({"image.png": b"\x89PNG", "doc.md": "# Doc\n\nContent."})
    items, _ = parse_zip(zip_bytes, source="md_upload")
    assert len(items) == 1
    assert items[0].title == "Doc"
```

- [x] **3.2** Implement:

```python
# backend/app/services/ingest/md_importer.py
"""Markdown zip importer (kb-ingestion-connectors, md_importer section).

Connector layer only: parses a zip of ``.md`` files into IngestItems and
wikilink EdgeSpecs. Persistence is owned by KnowledgeIngestor — never here.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from app.services.ingest.base import EdgeSpec, IngestItem

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
# [ \t] not \s: \s matches newlines ("#   \n\nbody" would title itself "body")
_HEADING_RE = re.compile(r"^#[ \t]+(.+)", re.MULTILINE)


def extract_wikilinks(body: str) -> list[str]:
    return _WIKILINK_RE.findall(body)


def _title_from_body_or_filename(body: str, filename: str) -> str:
    """Extract first H1 heading as title, fall back to filename stem."""
    m = _HEADING_RE.search(body)
    if m and m.group(1).strip():  # whitespace-only H1 falls back to filename
        return m.group(1).strip()
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()


def parse_zip(
    zip_bytes: bytes,
    source: str = "md_upload",
) -> tuple[list[IngestItem], list[EdgeSpec]]:
    """
    Parse a zip archive of Markdown files.
    Returns (items, edge_specs) ready for KnowledgeIngestor.
    """
    items: list[IngestItem] = []
    edge_specs: list[EdgeSpec] = []
    title_to_ref: dict[str, str] = {}  # title → source_ref (for wikilink resolution)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        md_files = [
            n for n in zf.namelist() if n.lower().endswith(".md") and not n.startswith("__MACOSX")
        ]

        for name in md_files:
            try:
                body = zf.read(name).decode("utf-8", errors="replace")
            except Exception:
                continue

            title = _title_from_body_or_filename(body, name)
            item = IngestItem(
                source=source,
                source_ref=name,
                title=title,
                body=body,
            )
            items.append(item)
            # First-wins on duplicate titles (deterministic wikilink resolution);
            # duplicates and unreadable members are logged, never silent.
            if title not in title_to_ref:
                title_to_ref[title] = name

    # Second pass: resolve wikilinks to source_refs
    for item in items:
        for linked_title in extract_wikilinks(item.body):
            target_ref = title_to_ref.get(linked_title)
            if target_ref and target_ref != item.source_ref:
                edge_specs.append(
                    EdgeSpec(
                        source_ref=item.source_ref,
                        target_ref=target_ref,
                        label="LINKS_TO",
                        props={"created_by": "wikilink"},
                    )
                )

    return items, edge_specs
```

- [x] **3.3** Run tests:
```bash
cd backend && pytest tests/services/ingest/test_md_importer.py -v
# Expected: 4 passed
```

- [x] **3.4** Commit:
```
feat(ingest): Markdown zip parser with wikilink edge extraction
```

---

## Task 4 — Ingest Celery task (idempotent)

**Files:**
- Create: `backend/app/workers/tasks/ingest_md.py`
- Create: `backend/tests/workers/test_ingest_md.py`

### Steps

- [ ] **4.1** Write failing tests (idempotency is mandatory):

```python
# backend/tests/workers/test_ingest_md.py
import io
import zipfile
import uuid
import pytest
from sqlalchemy import select, func
from app.models.knowledge import KnowledgeNode
from app.models.ingest import IngestionRun, RunStatus
from app.models.user import Role
from app.services.visibility import Viewer
from app.workers.tasks.ingest_md import _ingest_md_impl

pytestmark = pytest.mark.asyncio


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


async def test_ingest_creates_nodes(db, make_user):
    owner = await make_user(email="imdingest1@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = IngestionRun(id=uuid.uuid4(), owner_id=owner.id, source="md_upload", total_items=2)
    db.add(run)
    await db.flush()

    zip_bytes = make_zip({
        "note1.md": "# Alpha\n\nFirst note.",
        "note2.md": "# Beta\n\nSecond note.",
    })

    await _ingest_md_impl(db, run.id, zip_bytes, viewer)

    count = await db.scalar(
        select(func.count()).select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )
    assert count == 2

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.done


async def test_ingest_idempotent(db, make_user):
    """Ingesting the same zip twice must NOT create duplicate nodes."""
    owner = await make_user(email="imd_idem@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())

    zip_bytes = make_zip({"page.md": "# Same Page\n\nContent."})

    run1 = IngestionRun(id=uuid.uuid4(), owner_id=owner.id, source="md_upload", total_items=1)
    db.add(run1)
    await db.flush()
    await _ingest_md_impl(db, run1.id, zip_bytes, viewer)

    count1 = await db.scalar(
        select(func.count()).select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )

    run2 = IngestionRun(id=uuid.uuid4(), owner_id=owner.id, source="md_upload", total_items=1)
    db.add(run2)
    await db.flush()
    await _ingest_md_impl(db, run2.id, zip_bytes, viewer)

    count2 = await db.scalar(
        select(func.count()).select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )
    assert count1 == count2, "Idempotency violated: duplicate nodes created"
```

- [ ] **4.2** Implement:

```python
# backend/app/workers/tasks/ingest_md.py
from __future__ import annotations

import uuid
from datetime import datetime, UTC

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingest import IngestionRun, RunStatus
from app.services.ingest.base import KnowledgeIngestor
from app.services.ingest.md_importer import parse_zip
from app.services.visibility import Viewer
from app.workers.celery_app import celery_app, task_session


async def _ingest_md_impl(
    db: AsyncSession,
    run_id: uuid.UUID,
    zip_bytes: bytes,
    viewer: Viewer,
    progress_callback=None,
) -> None:
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None:
        return

    run.status = RunStatus.running
    await db.flush()

    try:
        items, edge_specs = parse_zip(zip_bytes, source="md_upload")
        run.total_items = len(items)
        await db.flush()

        ingestor = KnowledgeIngestor(db, viewer)

        for i, item in enumerate(items):
            await ingestor.upsert(item)
            for spec in edge_specs:
                if spec.source_ref == item.source_ref:
                    ingestor.add_edge_spec(spec)
            run.processed_items = i + 1
            await db.flush()
            if progress_callback:
                await progress_callback(i + 1, len(items))

        await ingestor.resolve_edges()

        run.status = RunStatus.done
        run.finished_at = datetime.now(UTC)
        await db.flush()

    except Exception as exc:
        run.status = RunStatus.failed
        run.error_log = str(exc)
        run.finished_at = datetime.now(UTC)
        await db.flush()
        raise


@celery_app.task(
    bind=True,
    name="kb.ingest_md",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
)
def ingest_md(self, run_id: str, zip_b64: str, user_id: str, role: str, group_ids: list[str]) -> None:
    import asyncio
    import base64
    from app.models.user import Role
    from app.services.visibility import Viewer

    zip_bytes = base64.b64decode(zip_b64)
    viewer = Viewer(
        user_id=uuid.UUID(user_id),
        role=Role(role),
        group_ids=frozenset(uuid.UUID(g) for g in group_ids),
    )

    async def _run():
        async with task_session() as db:
            await _ingest_md_impl(db, uuid.UUID(run_id), zip_bytes, viewer)

    try:
        asyncio.get_event_loop().run_until_complete(_run())
    except Exception as exc:
        raise self.retry(exc=exc)
```

- [ ] **4.3** Run tests:
```bash
cd backend && pytest tests/workers/test_ingest_md.py -v
# Expected: 2 passed (including idempotency)
```

- [ ] **4.4** Commit:
```
feat(workers): ingest_md Celery task — idempotent zip ingestion with IngestionRun tracking
```

---

## Task 5 — Upload API endpoint + WebSocket progress

**Files:**
- Create: `backend/app/api/v1/uploads.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_uploads_api.py`

### Steps

- [ ] **5.1** Write failing tests:

```python
# backend/tests/api/test_uploads_api.py
import io
import zipfile
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def make_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


async def test_upload_markdown_returns_202(client: AsyncClient, auth_headers):
    zip_bytes = make_zip_bytes({"hello.md": "# Hello\n\nContent."})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 202
    data = r.json()
    assert "run_id" in data
    assert data["status"] == "pending"


async def test_upload_requires_zip(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.txt", io.BytesIO(b"not a zip"), "text/plain")},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 422


async def test_get_run_status(client: AsyncClient, auth_headers):
    zip_bytes = make_zip_bytes({"test.md": "# Test\n\nBody."})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    run_id = r.json()["run_id"]
    r2 = await client.get(f"/api/v1/uploads/runs/{run_id}", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["id"] == run_id
```

- [ ] **5.2** Create the router:

```python
# backend/app/api/v1/uploads.py
from __future__ import annotations

import base64
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_viewer
from app.models.ingest import IngestionRun, RunStatus
from app.services.visibility import Viewer

router = APIRouter(prefix="/uploads", tags=["uploads"])

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


class RunOut(BaseModel):
    id: uuid.UUID
    status: RunStatus
    total_items: int
    processed_items: int
    failed_items: int
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class UploadStarted(BaseModel):
    run_id: uuid.UUID
    status: RunStatus


@router.post("/markdown", response_model=UploadStarted, status_code=status.HTTP_202_ACCEPTED)
async def upload_markdown(
    file: UploadFile = File(...),
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=422, detail="File must be a .zip archive of Markdown files")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 100 MB)")

    # Validate it's actually a zip
    import zipfile
    if not zipfile.is_zipfile(content.__class__(content)):
        raise HTTPException(status_code=422, detail="Not a valid zip file")

    run = IngestionRun(
        id=uuid.uuid4(),
        owner_id=viewer.user_id,
        source="md_upload",
        status=RunStatus.pending,
        total_items=0,
    )
    db.add(run)
    await db.commit()

    # Dispatch Celery task (args are primitives only)
    from app.workers.tasks.ingest_md import ingest_md
    ingest_md.delay(
        str(run.id),
        base64.b64encode(content).decode(),
        str(viewer.user_id),
        viewer.role.value,
        [str(g) for g in viewer.group_ids],
    )

    return UploadStarted(run_id=run.id, status=RunStatus.pending)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    from app.core.errors import ForbiddenError, NotFoundError
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None:
        raise NotFoundError(f"Run {run_id} not found")
    if run.owner_id != viewer.user_id:
        raise ForbiddenError("Not your run")
    return run


@router.websocket("/runs/{run_id}/progress")
async def run_progress_ws(
    run_id: uuid.UUID,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    """
    WebSocket endpoint: streams progress events for an ingestion run.
    Client receives JSON: {"processed": N, "total": M, "status": "..."}
    Polls DB every 500ms until run is done or failed.
    """
    import asyncio
    await websocket.accept()
    try:
        while True:
            run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
            if run is None:
                await websocket.send_json({"error": "Run not found"})
                break
            await websocket.send_json({
                "processed": run.processed_items,
                "total": run.total_items,
                "status": run.status.value,
            })
            if run.status in (RunStatus.done, RunStatus.failed):
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
```

- [ ] **5.3** Register in `main.py`:

```python
from app.api.v1 import uploads as uploads_router
app.include_router(uploads_router.router, prefix="/api/v1")
```

- [ ] **5.4** Run tests:
```bash
cd backend && pytest tests/api/test_uploads_api.py -v
# Expected: 3 passed
```

- [ ] **5.5** curl evidence:
```bash
# Create a test zip
cd /tmp && mkdir notes && echo "# First Note\n\nHello world." > notes/first.md
zip -r notes.zip notes/

curl -s -X POST http://localhost:8000/api/v1/uploads/markdown \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/notes.zip" | jq .

# Expected: {"run_id": "...", "status": "pending"}
```

- [ ] **5.6** Commit:
```
feat(api): POST /api/v1/uploads/markdown (202 + Celery), GET /runs/:id, WS progress
```

---

## Task 6 — Service token API

**Files:**
- Create: `backend/app/api/v1/tokens.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_tokens_api.py`

### Steps

- [ ] **6.1** Write failing tests:

```python
# backend/tests/api/test_tokens_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_create_token(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/tokens",
        json={"name": "confluence-sync", "scopes": ["ingest", "read"]},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert "token" in data  # raw token returned once
    assert "id" in data


async def test_list_tokens(client: AsyncClient, auth_headers):
    await client.post("/api/v1/tokens", json={"name": "t1", "scopes": ["read"]}, headers=auth_headers)
    r = await client.get("/api/v1/tokens", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()) >= 1


async def test_revoke_token(client: AsyncClient, auth_headers):
    r = await client.post("/api/v1/tokens", json={"name": "t2", "scopes": ["read"]}, headers=auth_headers)
    tid = r.json()["id"]
    r2 = await client.delete(f"/api/v1/tokens/{tid}", headers=auth_headers)
    assert r2.status_code == 204
```

- [ ] **6.2** Create `tokens.py`:

```python
# backend/app/api/v1/tokens.py
from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_viewer
from app.core.errors import ForbiddenError, NotFoundError
from app.models.ingest import ApiToken

router = APIRouter(prefix="/tokens", tags=["tokens"])
_hasher = PasswordHasher()


class TokenCreate(BaseModel):
    name: str
    scopes: list[str] = ["read"]


class TokenCreated(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    token: str  # raw token — shown once only


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    created_at: datetime
    revoked: bool

    model_config = {"from_attributes": True}


@router.post("", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    payload: TokenCreate,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    raw = secrets.token_urlsafe(32)
    token = ApiToken(
        id=uuid.uuid4(),
        owner_id=viewer.user_id,
        name=payload.name,
        token_hash=_hasher.hash(raw),
        scopes=payload.scopes,
    )
    db.add(token)
    await db.commit()
    return TokenCreated(id=token.id, name=token.name, scopes=token.scopes, token=raw)


@router.get("", response_model=list[TokenOut])
async def list_tokens(
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(ApiToken).where(ApiToken.owner_id == viewer.user_id, ApiToken.revoked == False)
    )
    return list(rows)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: uuid.UUID,
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    token = await db.scalar(select(ApiToken).where(ApiToken.id == token_id))
    if token is None:
        raise NotFoundError("Token not found")
    if token.owner_id != viewer.user_id:
        raise ForbiddenError("Not your token")
    token.revoked = True
    await db.commit()
```

- [ ] **6.3** Register in `main.py`:
```python
from app.api.v1 import tokens as tokens_router
app.include_router(tokens_router.router, prefix="/api/v1")
```

- [ ] **6.4** Run all tests + full gate:
```bash
cd backend && pytest tests/ -v --tb=short
ruff check .
mypy --strict app/services/ app/schemas/
```

- [ ] **6.5** Commit:
```
feat(api): POST/GET/DELETE /api/v1/tokens — service token management
```

---

## Phase 4 exit gate

```bash
cd backend
pytest tests/ --tb=short              # all green
ruff check .                          # clean
mypy --strict app/services/ app/schemas/  # clean

# Idempotency evidence:
pytest tests/workers/test_ingest_md.py::test_ingest_idempotent -v

# WS progress check (manual):
# wscat -c ws://localhost:8000/api/v1/uploads/runs/<run_id>/progress
# Expected: JSON progress events stream until status = "done"
```

Update `docs/plans/README.md` — Phase 4 Status → `Done`.
