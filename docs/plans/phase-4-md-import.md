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
- [x] All tasks checked
- [x] `pytest backend/tests/` green — `175 passed, 13 skipped` (Neo4j-unreachable
  sandbox skips; convert to passes on the Docker stack)
- [x] `ruff check backend/` + format clean; mypy strict clean (api/services/schemas/workers)
- [x] Idempotency: `pytest -k idempotent` → 5 passed (incl. `test_ingest_idempotent`:
  same zip twice → same node count)
- [x] WebSocket progress confirmed via integration test (§5.4: real server + real PG,
  streamed running→done frames; WS handshake authenticated + owner-scoped per 5.R.1)
- [x] curl evidence for `POST /api/v1/uploads/markdown` in §5.6 (202 + run JSON)

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

> [plan-fix] carried over from Task 2: the ingest contract QUEUES all graph ops
> on the session (PG-first, ADR-011) but this task's block never drained them —
> the per-batch checkpoint in `_run_ingest`/`_ingest_md_impl` drains them with
> `ns.run_pending_graph_ops(db)` right after each commit, INSIDE the open
> `task_session` (see 4.R.2; recorder tests prove vertices/edges only flow
> post-commit). Also: under the real `task_session` an exception ROLLS BACK the
> impl's in-transaction `status=failed` write, so `_mark_run_failed` re-marks
> the run in a fresh transaction before the error propagates for retry. Task
> shape mirrors `embed_node`: `queue="ingest"` (kb-celery-jobs rule 6),
> `retry_backoff=True` instead of `default_retry_delay`, and `asyncio.run`
> instead of the deprecated `get_event_loop().run_until_complete`.

**Files:**
- Create: `backend/app/workers/tasks/ingest_md.py`
- Create: `backend/tests/workers/test_ingest_md.py`
- Modify ([review-fix 4.R]): `backend/app/workers/celery_app.py`, `backend/tests/workers/test_celery_app.py`

### Steps

- [x] **4.1** Write failing tests (idempotency is mandatory; recorder tests per
  the [plan-fix] above, pattern from `tests/services/ingest/test_ingest_base.py`):

```python
# backend/tests/workers/test_ingest_md.py
"""Ingest Celery task: idempotent zip ingestion + IngestionRun tracking.

[review-fix 4.R]: per-item durability (mid-zip failure keeps committed nodes +
counters, resumable), and the fake task_session now exercises REAL close
semantics (separate session, logged commits, actual close)."""

import io
import uuid
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingest import IngestionRun, RunStatus
from app.models.knowledge import KnowledgeNode
from app.models.user import Role
from app.services import node_service as ns
from app.services.ingest.base import KnowledgeIngestor
from app.services.visibility import Viewer
from app.workers.tasks import ingest_md as ingest_md_module
from app.workers.tasks.ingest_md import (
    _ingest_md_impl,
    _mark_run_failed,
    _run_ingest,
    ingest_md,
)

pytestmark = pytest.mark.asyncio


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


async def _make_run(db, owner, total_items: int = 1) -> IngestionRun:
    run = IngestionRun(
        id=uuid.uuid4(), owner_id=owner.id, source="md_upload", total_items=total_items
    )
    db.add(run)
    await db.flush()
    return run


async def test_ingest_md_task_options():
    """Canonical kb-celery-jobs task shape: ingest queue, backoff retries, late acks."""
    assert ingest_md.queue == "ingest", "long-running ingestion must route to the ingest queue"
    assert ingest_md.retry_backoff is True
    assert ingest_md.acks_late is True
    assert ingest_md.max_retries == 2


async def test_ingest_creates_nodes(db, make_user):
    owner = await make_user(email="imdingest1@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner, total_items=2)

    zip_bytes = make_zip(
        {
            "note1.md": "# Alpha\n\nFirst note.",
            "note2.md": "# Beta\n\nSecond note.",
        }
    )

    await _ingest_md_impl(db, run.id, zip_bytes, viewer)

    count = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )
    assert count == 2

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.done
    assert run_result.processed_items == 2
    assert run_result.finished_at is not None


async def test_ingest_idempotent(db, make_user):
    """Ingesting the same zip twice must NOT create duplicate nodes."""
    owner = await make_user(email="imd_idem@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())

    zip_bytes = make_zip({"page.md": "# Same Page\n\nContent."})

    run1 = await _make_run(db, owner)
    await _ingest_md_impl(db, run1.id, zip_bytes, viewer)

    count1 = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )

    run2 = await _make_run(db, owner)
    await _ingest_md_impl(db, run2.id, zip_bytes, viewer)

    count2 = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner.id, KnowledgeNode.source == "md_upload")
    )
    assert count1 == count2, "Idempotency violated: duplicate nodes created"


async def test_ingest_missing_run_is_noop(db, make_user):
    """Run row gone (race): tolerate, don't crash (kb-celery-jobs task shape)."""
    owner = await make_user(email="imd_norun@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await _ingest_md_impl(db, uuid.uuid4(), make_zip({"a.md": "# A"}), viewer)

    count = await db.scalar(
        select(func.count()).select_from(KnowledgeNode).where(KnowledgeNode.owner_id == owner.id)
    )
    assert count == 0


async def test_ingest_failure_marks_run_failed_and_raises(db, make_user):
    """A bad zip must set status=failed + error_log and re-raise for Celery retry."""
    owner = await make_user(email="imd_fail@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner)

    with pytest.raises(zipfile.BadZipFile):
        await _ingest_md_impl(db, run.id, b"this is not a zip", viewer)

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert run_result.finished_at is not None


# --- [plan-fix] PG-first invariant: graph ops flow only AFTER commit (ADR-011) ---
# Recorder pattern from tests/services/ingest/test_ingest_base.py — no live Neo4j.


def _graph_recorder(monkeypatch, log: list[tuple[str, ...]]) -> None:
    from app.services import graph_service as gs

    async def fake_upsert(node):
        log.append(("vertex", str(node.id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        log.append(("edge", str(source_id), str(target_id), label))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)


def _fake_task_session(db, log: list[tuple[str, ...]]):
    """[review-fix 4.R] Mirror REAL task_session semantics on the test
    connection: a SEPARATE AsyncSession whose commits release savepoints on the
    test's outer transaction (join_transaction_mode="create_savepoint", so
    'durable' work still rolls back with the test), a logged ("commit",) marker
    per commit, commit-on-clean-exit, and a REAL close (("close",) marker).

    The previous fake yielded the long-lived test session inside one savepoint:
    it could not represent mid-block batch commits and kept the session
    artificially open after the context exited — hiding that the worker drained
    graph ops on an already-closed session."""

    @asynccontextmanager
    async def fake():
        conn = await db.connection()
        inner = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        real_commit = inner.commit

        async def logged_commit() -> None:
            await real_commit()
            log.append(("commit",))

        inner.commit = logged_commit  # type: ignore[method-assign]
        try:
            yield inner
            await inner.commit()  # the real task_session commits on clean exit
        finally:
            await inner.close()
            log.append(("close",))

    return fake


async def test_impl_queues_graph_ops_without_running_them(db, make_user, monkeypatch):
    """_ingest_md_impl runs in-transaction: it must only QUEUE graph ops."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    owner = await make_user(email="imd_queue@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner, total_items=2)

    zip_bytes = make_zip(
        {
            "a.md": "# A\n\nsee [[B]]",
            "b.md": "# B\n\ncontent",
        }
    )
    await _ingest_md_impl(db, run.id, zip_bytes, viewer)

    assert log == [], "nothing may hit Neo4j inside the transaction (ADR-011)"
    await ns.run_pending_graph_ops(db)
    assert any(e[0] == "vertex" for e in log)
    assert any(e[0] == "edge" and e[3] == "LINKS_TO" for e in log)


async def test_run_ingest_drains_graph_ops_post_commit(db, make_user, monkeypatch):
    """[review-fix 4.R] worker orchestration: graph ops flow batch-by-batch —
    each drain AFTER its batch's commit and BEFORE the task session closes."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    monkeypatch.setattr(ingest_md_module, "task_session", _fake_task_session(db, log))

    owner = await make_user(email="imd_drain@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run = await _make_run(db, owner, total_items=2)

    zip_bytes = make_zip(
        {
            "x.md": "# X\n\nsee [[Y]]",
            "y.md": "# Y\n\ncontent",
        }
    )
    await _run_ingest(run.id, zip_bytes, viewer)

    commit_idxs = [i for i, e in enumerate(log) if e == ("commit",)]
    close_idx = log.index(("close",))
    graph_idxs = [i for i, e in enumerate(log) if e[0] in ("vertex", "edge")]
    assert graph_idxs, "the worker must drain the queued graph ops"
    assert all(i > min(commit_idxs) for i in graph_idxs), (
        "graph ops ran before the first commit boundary (ADR-011 violation)"
    )
    assert all(i < close_idx for i in graph_idxs), (
        "graph ops must drain while the task session is still open, not after close"
    )
    assert any(i < max(commit_idxs) for i in graph_idxs), (
        "graph ops must flow batch-by-batch, not lumped after the final commit"
    )
    assert any(log[i][0] == "edge" for i in graph_idxs), "wikilink edge must be merged"


async def test_run_ingest_marks_run_failed_in_fresh_tx(db, make_user, monkeypatch):
    """[plan-fix] the impl's in-tx failed write rolls back with the transaction;
    _run_ingest must persist status=failed in a fresh transaction, then re-raise."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    monkeypatch.setattr(ingest_md_module, "task_session", _fake_task_session(db, log))

    owner = await make_user(email="imd_failtx@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    run_id = (await _make_run(db, owner)).id

    with pytest.raises(zipfile.BadZipFile):
        await _run_ingest(run_id, b"not a zip", viewer)

    db.expire_all()  # the fake task session updated the row behind this session
    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert all(e[0] not in ("vertex", "edge") for e in log), (
        "graph ops from a rolled-back transaction must never run"
    )


async def test_run_ingest_failure_mid_zip_keeps_committed_progress(db, make_user, monkeypatch):
    """[review-fix 4.R, kb-celery-jobs rule 5] per-item durability: items
    committed before a mid-zip failure persist — their nodes AND the
    processed_items counter — and the run is marked failed WITH the accumulated
    counts. A re-run of the same zip then converges idempotently (content-hash
    skip), making the job resumable."""
    log: list[tuple[str, ...]] = []
    _graph_recorder(monkeypatch, log)
    monkeypatch.setattr(ingest_md_module, "task_session", _fake_task_session(db, log))

    real_upsert = KnowledgeIngestor.upsert

    async def flaky_upsert(self, item):
        if item.source_ref == "b.md":
            raise RuntimeError("injected failure on item 2")
        return await real_upsert(self, item)

    monkeypatch.setattr(KnowledgeIngestor, "upsert", flaky_upsert)

    owner = await make_user(email="imd_midzip@test.com")
    owner_id = owner.id  # captured: expire_all() below makes attribute access lazy-load
    viewer = Viewer(user_id=owner_id, role=Role.user, group_ids=frozenset())
    run_id = (await _make_run(db, owner)).id

    zip_bytes = make_zip(
        {
            "a.md": "# A\n\nfirst",
            "b.md": "# B\n\nsecond",
            "c.md": "# C\n\nthird",
        }
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        await _run_ingest(run_id, zip_bytes, viewer)

    db.expire_all()
    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log
    assert run_result.processed_items == 1, (
        "counters committed before the failure must survive it (rule 5)"
    )

    titles = (
        await db.scalars(
            select(KnowledgeNode.title).where(
                KnowledgeNode.owner_id == owner_id, KnowledgeNode.source == "md_upload"
            )
        )
    ).all()
    assert titles == ["A"], "nodes committed before the failure must persist"

    # Resumable: re-running the (now healthy) zip converges without duplicates.
    monkeypatch.setattr(KnowledgeIngestor, "upsert", real_upsert)
    await db.refresh(owner)  # expired above; _make_run reads owner.id
    run2_id = (await _make_run(db, owner)).id
    await _run_ingest(run2_id, zip_bytes, viewer)

    db.expire_all()
    count = await db.scalar(
        select(func.count())
        .select_from(KnowledgeNode)
        .where(KnowledgeNode.owner_id == owner_id, KnowledgeNode.source == "md_upload")
    )
    assert count == 3, "re-run must converge: A skipped by content hash, B and C created"
    run2_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run2_id))
    assert run2_result.status == RunStatus.done
    assert run2_result.processed_items == 3


async def test_mark_run_failed(db, make_user):
    owner = await make_user(email="imd_mark@test.com")
    run = await _make_run(db, owner)

    await _mark_run_failed(db, run.id, "boom")

    run_result = await db.scalar(select(IngestionRun).where(IngestionRun.id == run.id))
    assert run_result.status == RunStatus.failed
    assert run_result.error_log == "boom"
    assert run_result.finished_at is not None


async def test_mark_run_failed_missing_run_is_noop(db):
    await _mark_run_failed(db, uuid.uuid4(), "boom")  # must not raise
```

- [x] **4.2** Implement (per the [plan-fix] above: `_run_ingest` drains graph ops
  post-commit and re-marks failures in a fresh transaction; task shape mirrors
  `embed_node`):

```python
# backend/app/workers/tasks/ingest_md.py
"""Celery task: idempotent Markdown zip ingestion with IngestionRun tracking.

PG first, Neo4j second (ADR-011): _ingest_md_impl only QUEUES graph ops (via
KnowledgeIngestor / node_service); the per-batch checkpoint drains them with
run_pending_graph_ops right AFTER each commit, while the session is still open.

Durability ([review-fix 4.R], kb-celery-jobs rule 5): the run commits every
_COMMIT_EVERY items, so nodes and the processed_items counter survive a
mid-zip failure — the counts Task 5 status readers see are real, and a re-run
converges from where the last commit left off (content-hash skip): resumable.

Idempotency (kb-celery-jobs rule 1): KnowledgeIngestor.upsert is keyed on
(owner_id, source, source_ref) with a content-hash short-circuit, so re-running
the task on the same zip creates zero new nodes.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from celery import Task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingest import IngestionRun, RunStatus
from app.models.user import Role
from app.services import node_service as ns
from app.services.ingest.base import KnowledgeIngestor
from app.services.ingest.md_importer import parse_zip
from app.services.visibility import Viewer
from app.workers.celery_app import celery_app, task_session

# Progress hook (processed, total) — Task 5 wires this to Redis pub/sub
# channel run:{ingestion_run_id} (kb-celery-jobs rule 8).
ProgressCallback = Callable[[int, int], Awaitable[None]]

# Durability checkpoint: commit accumulated work, then drain queued graph ops.
Checkpoint = Callable[[AsyncSession], Awaitable[None]]

# Items per durability commit (kb-celery-jobs rule 5). 1 because items are
# whole Markdown files (coarse units) and the Task 5 WS reader polls
# processed_items — a bigger batch would freeze visible progress and lose more
# work on a crash. Raise only with evidence that commit overhead matters.
_COMMIT_EVERY = 1


async def _checkpoint(db: AsyncSession) -> None:
    """[review-fix 4.R] Durability boundary: commit the accumulated batch, then
    drain the queued graph ops POST-commit while the session is still open
    (ADR-011 + kb-celery-jobs rule 5). Neo4j stays best-effort — each op is
    wrapped in _graph_sync, so a graph failure never undoes the committed
    batch (a Celery retry re-converges the graph)."""
    await db.commit()
    await ns.run_pending_graph_ops(db)


async def _ingest_md_impl(
    db: AsyncSession,
    run_id: uuid.UUID,
    zip_bytes: bytes,
    viewer: Viewer,
    progress_callback: ProgressCallback | None = None,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Core logic, testable without a broker. Graph ops are only queued here,
    never awaited (ADR-011). `checkpoint` (commit + post-commit drain, provided
    by _run_ingest) runs every _COMMIT_EVERY items and once after edge
    resolution; without one (in-transaction unit tests) work is only flushed
    and stays inside the caller's transaction."""
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
            # Durability boundary (rule 5): everything up to and including
            # this item — nodes, counters, queued vertex syncs — becomes
            # permanent now, so a later failure cannot take it back.
            if checkpoint is not None and (i + 1) % _COMMIT_EVERY == 0:
                await checkpoint(db)
            else:
                await db.flush()
            if progress_callback:
                await progress_callback(i + 1, len(items))

        await ingestor.resolve_edges()

        run.status = RunStatus.done
        run.finished_at = datetime.now(UTC)
        # Final checkpoint: the tail batch, the resolved edges and the done
        # status must be durable and drained BEFORE the session closes.
        if checkpoint is not None:
            await checkpoint(db)
        else:
            await db.flush()

    except Exception as exc:
        # Best effort for callers running everything in one transaction (unit
        # tests). Under _run_ingest this uncommitted write is discarded on
        # close and _mark_run_failed re-marks the run in a fresh session —
        # KEEPING the counters committed by earlier checkpoints.
        run.status = RunStatus.failed
        run.error_log = str(exc)
        run.finished_at = datetime.now(UTC)
        await db.flush()
        raise


async def _mark_run_failed(db: AsyncSession, run_id: uuid.UUID, error: str) -> None:
    """[plan-fix] persist the failure OUTSIDE the rolled-back ingest transaction,
    so Celery retries and Task 5 status readers see status=failed. Touches
    status/error/finished_at ONLY: processed_items keeps the counts accumulated
    by the committed batches ([review-fix 4.R] — 'failed after N of M')."""
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None:
        return
    run.status = RunStatus.failed
    run.error_log = error
    run.finished_at = datetime.now(UTC)
    await db.flush()


async def _run_ingest(run_id: uuid.UUID, zip_bytes: bytes, viewer: Viewer) -> None:
    """Session orchestration for the task. The impl checkpoints (commit + drain)
    batch-by-batch INSIDE the open task_session — nothing graph-related runs
    after the session closes ([review-fix 4.R]: the first cut drained once
    after the context exited, which lumped every graph op at the end and only
    worked because expire_on_commit=False left the closed session readable).
    On failure the current batch rolls back with the session, committed batches
    (nodes + counters) persist, and the run is re-marked failed in a fresh
    session before the error propagates for retry."""
    try:
        async with task_session() as db:
            await _ingest_md_impl(db, run_id, zip_bytes, viewer, checkpoint=_checkpoint)
    except Exception as exc:
        async with task_session() as fail_db:
            await _mark_run_failed(fail_db, run_id, str(exc))
        raise


@celery_app.task(  # type: ignore[untyped-decorator]  # celery is untyped (ignore_missing_imports)
    bind=True,
    name="kb.ingest_md",
    queue="ingest",  # long-running batch work (kb-celery-jobs rule 6); workers consume -Q ingest
    acks_late=True,
    max_retries=2,
    retry_backoff=True,
)
def ingest_md(
    self: Task, run_id: str, zip_b64: str, user_id: str, role: str, group_ids: list[str]
) -> None:
    """Celery task: ingest a zip of Markdown files for the given viewer.
    Args are primitives only (kb-celery-jobs rule 2); the zip travels base64.
    Idempotent — safe to re-run on at-least-once delivery."""
    zip_bytes = base64.b64decode(zip_b64)
    viewer = Viewer(
        user_id=uuid.UUID(user_id),
        role=Role(role),
        group_ids=frozenset(uuid.UUID(g) for g in group_ids),
    )

    try:
        asyncio.run(_run_ingest(uuid.UUID(run_id), zip_bytes, viewer))
    except Exception as exc:
        raise self.retry(exc=exc) from exc
```

- [x] **4.3** Run tests:
```bash
cd backend && pytest tests/workers/test_ingest_md.py tests/workers/test_celery_app.py -v
# Expected: 15 passed — 11 in test_ingest_md.py (idempotency, batch-drain and
# mid-zip durability recorder tests) + 4 in test_celery_app.py
```

- [x] **4.4** Commit:
```
feat(workers): ingest_md Celery task — idempotent zip ingestion with IngestionRun tracking
```

### 4.R Review fixes (on commit 778ca42)

- [x] **4.R.1 CRITICAL — per-item durability (kb-celery-jobs rule 5).** The first
  cut ran the whole zip in ONE transaction: a mid-zip failure rolled back every
  node upsert AND the processed_items counters, `_mark_run_failed` wrote status
  only — so "failed with counts so far" was false and nothing was resumable.
  Fixed: `_ingest_md_impl` takes a `checkpoint` (commit + post-commit graph-op
  drain) injected by `_run_ingest` and invokes it every `_COMMIT_EVERY = 1`
  items and after edge resolution. Per-item (N=1) because items are whole
  Markdown files and the Task 5 WS reader polls `processed_items` — larger
  batches would freeze visible progress. Committed batches survive failure;
  `_mark_run_failed` keeps the accumulated counters; re-runs converge via the
  content-hash skip (test: `test_run_ingest_failure_mid_zip_keeps_committed_progress`).
  This required `task_session` to stop wrapping the block in `session.begin()`
  (a mid-block commit closed the enclosing transaction; the next statement
  raised InvalidRequestError — verified RED by
  `test_task_session_supports_mid_block_batch_commit`). New shape, identical
  commit-on-clean-exit / rollback-on-error semantics for existing tasks:

```python
# backend/app/workers/celery_app.py
@asynccontextmanager
async def task_session() -> AsyncIterator[AsyncSession]:
    from app.core.db import SessionLocal

    async with SessionLocal() as session:
        yield session
        await session.commit()
```

- [x] **4.R.2 IMPORTANT — drain graph ops while the session is open.** The first
  cut called `ns.run_pending_graph_ops(db)` AFTER the `task_session` context
  closed — it only worked because `expire_on_commit=False` left the closed
  session readable, and it lumped every graph op at the end. Fixed: the
  checkpoint drains right after each batch commit inside the open session
  (graph ops flow batch-by-batch). The recorder fake `_fake_task_session` was
  rewritten to exercise REAL close semantics (separate `AsyncSession` on the
  test connection with `join_transaction_mode="create_savepoint"`, logged
  commits, actual close); the drain test asserts ops land after a commit and
  strictly before the ("close",) marker.

- [x] **4.R.3 NIT — plan note (no code change).** The zip travels base64 through
  the Redis broker; at the Task 5 cap of 100 MB that is ~133 MB per message
  held in broker memory (and re-delivered on every retry). Acceptable for the
  self-hosted single-team scale now, but flagged as a **Phase 7 hardening
  candidate**: upload the zip to MinIO first (`app/services/storage.py` already
  exists from Task 1) and pass only the object path as the task arg.

---

## Task 5 — Upload API endpoint + WebSocket progress

> [plan-note, from 4.R.3] `ingest_md.delay(..., base64(zip))` pushes up to
> ~133 MB per message through Redis at the 100 MB cap. Phase 7 hardening
> candidate: store the upload in MinIO (Task 1's `storage.upload_file`) and
> pass the object path instead of the base64 payload.

**Files:**
- Create: `backend/app/api/v1/uploads.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_uploads_api.py`
- Modify: `backend/pyproject.toml` ([plan-fix] `python-multipart` — required by
  `UploadFile` form parsing, never declared)

### Steps

- [x] **5.1** Write failing tests ([plan-fix] vs the original block: `ingest_md.delay`
  is monkeypatched to a recorder — kb-celery-jobs forbids a live broker in unit
  tests — and the 202 test asserts the enqueued primitive args; the
  `Content-Type` dict-comp was a no-op (the fixture only carries Authorization)
  and is dropped; added the kb-api-conventions checklist tests (401, cross-user
  404-generic, bad-content 422) and sync WS integration tests via starlette's
  TestClient against the real DB, because httpx ASGITransport — the `client`
  fixture — cannot speak WebSocket; rows are committed and cleaned up):

```python
# backend/tests/api/test_uploads_api.py  (as built — see file for full docstring)
import asyncio
import base64
import concurrent.futures
import io
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.ingest import IngestionRun, RunStatus
from app.models.user import Role, User

# No module-level asyncio pytestmark: asyncio_mode="auto" already collects the
# async tests, and the mark would mis-tag the sync WS integration test.


def make_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def recorded_delay(monkeypatch):
    """Record ingest_md enqueues instead of publishing to the broker."""
    from app.workers.tasks.ingest_md import ingest_md

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(ingest_md, "delay", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


# --- POST /api/v1/uploads/markdown ---


async def test_upload_markdown_returns_202(client: AsyncClient, auth_headers, recorded_delay):
    zip_bytes = make_zip_bytes({"hello.md": "# Hello\n\nContent."})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=auth_headers,
    )
    assert r.status_code == 202
    data = r.json()
    assert "run_id" in data
    assert data["status"] == "pending"

    # The task got the committed run id and primitive viewer args (rule 2).
    assert len(recorded_delay) == 1
    args, kwargs = recorded_delay[0]
    assert kwargs == {}
    run_id, zip_b64, user_id, role, group_ids = args
    assert run_id == data["run_id"]
    assert base64.b64decode(zip_b64) == zip_bytes
    uuid.UUID(user_id)  # a plain str uuid, not an ORM object
    assert role == "user"
    assert group_ids == []


async def test_upload_requires_zip(client: AsyncClient, auth_headers, recorded_delay):
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.txt", io.BytesIO(b"not a zip"), "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert recorded_delay == []


async def test_upload_rejects_invalid_zip_content(
    client: AsyncClient, auth_headers, recorded_delay
):
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(b"zip by name only"), "application/zip")},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert recorded_delay == []


async def test_upload_unauthenticated_is_401(client: AsyncClient):
    zip_bytes = make_zip_bytes({"a.md": "# A"})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
    )
    assert r.status_code == 401


# --- GET /api/v1/uploads/runs/{run_id} ---


async def test_get_run_status(client: AsyncClient, auth_headers):
    zip_bytes = make_zip_bytes({"test.md": "# Test\n\nBody."})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=auth_headers,
    )
    run_id = r.json()["run_id"]
    r2 = await client.get(f"/api/v1/uploads/runs/{run_id}", headers=auth_headers)
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == run_id
    assert body["status"] == "pending"
    assert body["processed_items"] == 0


async def test_get_run_of_another_user_is_404(
    client: AsyncClient, auth_headers, auth_headers_other
):
    """Invisible == nonexistent: a 403 would confirm the run id exists."""
    zip_bytes = make_zip_bytes({"secret.md": "# Secret"})
    r = await client.post(
        "/api/v1/uploads/markdown",
        files={"file": ("notes.zip", io.BytesIO(zip_bytes), "application/zip")},
        headers=auth_headers,
    )
    run_id = r.json()["run_id"]
    r2 = await client.get(f"/api/v1/uploads/runs/{run_id}", headers=auth_headers_other)
    assert r2.status_code == 404
    assert r2.json()["detail"] == "Run not found"  # generic body, nothing confirmed


async def test_get_run_missing_is_404(client: AsyncClient, auth_headers):
    r = await client.get(f"/api/v1/uploads/runs/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404


# --- WS /api/v1/uploads/runs/{run_id}/progress (integration, real DB) ---


def _recv_json(ws, timeout: float = 10.0) -> Any:
    """receive_json with a watchdog — a silent server must fail the test, not hang it."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(ws.receive_json).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)


def _run_db(fn: Callable[[AsyncSession], Awaitable[None]]) -> None:
    """Run one committed unit of work on the real DB in a throwaway loop/engine."""

    async def _go() -> None:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with AsyncSession(engine) as session:
                await fn(session)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_go())


# fastapi re-exports starlette.testclient, which warns about httpx2 at import;
# environmental noise, not ours to fix here.
@pytest.mark.filterwarnings("ignore:Using `httpx` with `starlette.testclient`")
def test_ws_progress_streams_until_done():
    """Progress events flow over the WS while the run advances, ending at done."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    user_id, run_id = uuid.uuid4(), uuid.uuid4()

    async def _setup(s: AsyncSession) -> None:
        s.add(
            User(
                id=user_id,
                email=f"ws-{uuid.uuid4().hex[:8]}@test.com",
                password_hash="x",
                display_name="ws-test",
                role=Role.user,
            )
        )
        await s.flush()  # no ORM relationship → order the FK parent explicitly
        s.add(
            IngestionRun(
                id=run_id,
                owner_id=user_id,
                source="md_upload",
                status=RunStatus.running,
                total_items=3,
                processed_items=1,
            )
        )

    async def _advance(s: AsyncSession) -> None:
        run = await s.get(IngestionRun, run_id)
        run.processed_items = 3
        run.status = RunStatus.done

    async def _cleanup(s: AsyncSession) -> None:
        await s.execute(delete(IngestionRun).where(IngestionRun.id == run_id))
        await s.execute(delete(User).where(User.id == user_id))

    # [review-fix 5.R.1] the owner now authenticates the handshake; TestClient
    # apps get a NullPool get_db override (_ws_app) because asyncpg connections
    # are event-loop-bound; _ws_connect tolerates server-initiated closes.
    _run_db(_setup)
    token = make_access_token(user_id, "user")
    url = f"/api/v1/uploads/runs/{run_id}/progress"
    try:
        with TestClient(_ws_app()) as tc:
            with _ws_connect(tc, f"{url}?token={token}") as ws:
                first = _recv_json(ws)
                assert first == {"processed": 1, "total": 3, "status": "running"}

                _run_db(_advance)  # the "worker" commits progress behind the WS session

                evt = _recv_json(ws)
                while evt["status"] == "running":
                    evt = _recv_json(ws)
                assert evt == {"processed": 3, "total": 3, "status": "done"}

            # cookie-auth handshake (browser through the Next.js BFF: cookies
            # flow on the same-origin WS upgrade): run is done → final frame
            with _ws_connect(tc, url, headers={"cookie": f"access_token={token}"}) as ws:
                assert _recv_json(ws)["status"] == "done"

            # unknown run (authenticated): generic 4404 close, no frame —
            # indistinguishable from someone else's run
            with _ws_connect(
                tc, f"/api/v1/uploads/runs/{uuid.uuid4()}/progress?token={token}"
            ) as ws:
                _expect_ws_close(ws, 4404)
    finally:
        _run_db(_cleanup)


# [review-fix 5.R.1] plus test_ws_progress_auth_required_and_ownership_enforced
# (see the file): no/garbage token → close 1008 (policy violation); a VALID
# token for another user → generic close 4404 with no progress frame leaked.
```

- [x] **5.2** Create the router ([plan-fix] vs the original block — details in the
  module docstring: `get_scoped_viewer` not `get_current_viewer` (admin bypass
  only under /api/v1/admin/*); `zipfile.is_zipfile` needs a file-like object,
  the plan passed raw bytes (`content.__class__(content)`), which it cannot
  read; get_run answers another user's run 404-generic (invisible ==
  nonexistent) instead of 403, which confirms the run id exists; the WS poll
  re-selects with `populate_existing=True` or the session identity map would
  replay the first read forever and never show worker progress;
  summary/operation_id added; plain `file: UploadFile` (no `File(...)` default —
  ruff B008); `RunOut.model_validate(run)` — routers never return ORM objects.
  Also [plan-fix]: `python-multipart` added to pyproject dependencies —
  `UploadFile` form parsing requires it and the plan never declared it):

```python
# backend/app/api/v1/uploads.py  (as built)
"""Uploads router — POST a zip of Markdown files, ingest asynchronously (Celery),
track the run, and stream progress over WebSocket.

The run-status probes here are sanctioned raw queries (daily_logs precedent):
`ingestion_runs` is upload bookkeeping owned by exactly one user, not a
knowledge read path — ownership (owner_id == viewer.user_id) is the whole
visibility rule, answered 404-generic like any invisible read.

> Scale note (Task 5 plan blockquote): `ingest_md.delay(..., base64(zip))`
> pushes up to ~133 MB per message through Redis at the 100 MB cap. Phase 7
> hardening candidate: store the upload in MinIO (`storage.upload_file`) and
> pass the object path instead of the payload. Do not redesign here.
"""

from __future__ import annotations

import asyncio
import base64
import io
import uuid
import zipfile
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer, get_ws_viewer
from app.core.errors import NotFoundError
from app.models.ingest import IngestionRun, RunStatus
from app.services.ingest.md_importer import check_zip_limits
from app.workers.tasks.ingest_md import ingest_md

router = APIRouter(prefix="/uploads", tags=["uploads"])

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_WS_POLL_SECONDS = 0.5
# Application close code for "run invisible OR nonexistent" — one code for
# both, like get_run's generic 404 (a distinct code would confirm the id).
_WS_4404_NOT_FOUND = 4404


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


@router.post(
    "/markdown",
    response_model=UploadStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a zip of Markdown files for ingestion",
    operation_id="uploadMarkdown",
)
async def upload_markdown(
    file: UploadFile,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> UploadStarted:
    # Request-shape validation (not domain logic), so HTTPException is correct here.
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=422, detail="File must be a .zip archive of Markdown files")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 100 MB)")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # [review-fix 5.R.3] zip-bomb caps (declared decompressed size,
            # member count) enforced at the door — same guard parse_zip runs.
            check_zip_limits(zf)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="Not a valid zip file") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run = IngestionRun(
        id=uuid.uuid4(),
        owner_id=viewer.user_id,
        source="md_upload",
        status=RunStatus.pending,
        total_items=0,
    )
    db.add(run)
    await db.commit()  # the worker reads this row — it must be durable before enqueue

    # Primitive args only (kb-celery-jobs rule 2); the zip travels base64.
    try:
        ingest_md.delay(
            str(run.id),
            base64.b64encode(content).decode(),
            str(viewer.user_id),
            viewer.role.value,
            [str(g) for g in viewer.group_ids],
        )
    except Exception as exc:
        # [review-fix 5.R.4] enqueue-then-crash: the run row is already durable
        # but no worker will ever pick it up — a permanent "pending" lie. Mark
        # it failed in a fresh commit and tell the client to retry.
        run.status = RunStatus.failed
        run.error_log = f"enqueue failed: {exc}"
        run.finished_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=503, detail="ingestion queue unavailable, try again later"
        ) from exc

    return UploadStarted(run_id=run.id, status=RunStatus.pending)


@router.get(
    "/runs/{run_id}",
    response_model=RunOut,
    summary="Get ingestion run status",
    operation_id="getIngestionRun",
)
async def get_run(
    run_id: uuid.UUID,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> RunOut:
    run = await db.scalar(select(IngestionRun).where(IngestionRun.id == run_id))
    if run is None or run.owner_id != viewer.user_id:
        # Generic body: invisible == nonexistent, nothing confirmed either way.
        raise NotFoundError("Run not found")
    return RunOut.model_validate(run)


@router.websocket("/runs/{run_id}/progress")
async def run_progress_ws(
    run_id: uuid.UUID,
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stream progress events for an ingestion run (owner only).

    Client receives JSON: {"processed": N, "total": M, "status": "..."}.
    Polls the DB every 500 ms (the worker checkpoints processed_items per item)
    until the run is done or failed.

    [review-fix 5.R.1] The handshake is authenticated (`?token=` or the BFF's
    `access_token` cookie — see get_ws_viewer) and ownership is enforced
    BEFORE any frame is sent: no/bad credentials → 1008 policy violation;
    another user's run or an unknown id → generic 4404 close.

    [review-fix 5.R.2, documented deviation] kb-celery-jobs rule 8 prescribes a
    Redis pub/sub relay (`run:{id}` WsEvent channel); this endpoint polls PG
    instead — correct because the worker checkpoint-commits per item, and one
    less moving part. The pub/sub relay is the Phase 7 hardening upgrade path,
    alongside the MinIO payload offload (4.R.3).
    """
    await websocket.accept()
    try:
        viewer = await get_ws_viewer(websocket, db)
        if viewer is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        while True:
            run = await db.scalar(
                select(IngestionRun)
                .where(IngestionRun.id == run_id)
                # populate_existing: refresh identity-map attributes each poll,
                # or the loop would replay the first read forever [plan-fix].
                .execution_options(populate_existing=True)
            )
            # Ownership re-checked every poll: covers the handshake AND a run
            # deleted mid-stream, with the same generic close either way.
            if run is None or run.owner_id != viewer.user_id:
                await websocket.close(code=_WS_4404_NOT_FOUND)
                return
            await websocket.send_json(
                {
                    "processed": run.processed_items,
                    "total": run.total_items,
                    "status": run.status.value,
                }
            )
            if run.status in (RunStatus.done, RunStatus.failed):
                break
            await asyncio.sleep(_WS_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    await websocket.close()
```

- [x] **5.3** Register in `main.py` ([plan-fix] codebase import style — named
  router import, alphabetical):

```python
from app.api.v1.uploads import router as uploads_router
...
app.include_router(uploads_router, prefix="/api/v1")
```

- [x] **5.4** Run tests:
```bash
cd backend && pytest tests/api/test_uploads_api.py -v
# Expected: 11 passed  ([plan-fix] was "3 passed"; see 5.1 for the added tests,
# then 5.R: +1 WS auth/ownership, +1 zip-bomb 422, +1 enqueue-failure 503)
```

Evidence (sandbox, 2026-07-24 — RED first: all failed 404/WebSocketDisconnect
before 5.2/5.3 existed):

```text
tests/api/test_uploads_api.py::test_upload_markdown_returns_202 PASSED   [ 12%]
tests/api/test_uploads_api.py::test_upload_requires_zip PASSED           [ 25%]
tests/api/test_uploads_api.py::test_upload_rejects_invalid_zip_content PASSED [ 37%]
tests/api/test_uploads_api.py::test_upload_unauthenticated_is_401 PASSED [ 50%]
tests/api/test_uploads_api.py::test_get_run_status PASSED                [ 62%]
tests/api/test_uploads_api.py::test_get_run_of_another_user_is_404 PASSED [ 75%]
tests/api/test_uploads_api.py::test_get_run_missing_is_404 PASSED        [ 87%]
tests/api/test_uploads_api.py::test_ws_progress_streams_until_done PASSED [100%]
8 passed in 2.90s
```

`test_ws_progress_streams_until_done` is the WS exit-criterion evidence
(integration test in lieu of wscat): a real server streamed
`{"processed": 1, "total": 3, "status": "running"}` →
`{"processed": 3, "total": 3, "status": "done"}` as the run advanced.

- [x] **5.5** curl evidence (live uvicorn against sandbox PG, 2026-07-24;
  evidence rows + Redis queue cleaned up afterwards):

```bash
cd /tmp && mkdir notes && printf "# First Note\n\nHello world.\n" > notes/first.md
zip -r notes.zip notes/

curl -s -X POST http://localhost:8000/api/v1/uploads/markdown \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/notes.zip"
# → {"run_id":"a7fff7c7-2000-43c4-bbbc-88d2f9ff1212","status":"pending"}

curl -s http://localhost:8000/api/v1/uploads/runs/a7fff7c7-2000-43c4-bbbc-88d2f9ff1212 \
  -H "Authorization: Bearer $TOKEN"
# → {"id":"a7fff7c7-2000-43c4-bbbc-88d2f9ff1212","status":"pending","total_items":0,
#    "processed_items":0,"failed_items":0,
#    "created_at":"2026-07-25T03:28:56.950584Z","finished_at":null}
# (status stays pending: no Celery worker runs in the sandbox)
```

- [x] **5.6** Commit:
```
feat(api): POST /api/v1/uploads/markdown (202 + Celery), GET /runs/:id, WS progress
```

### 5.R Review fixes (on commit 67ed384)

- [x] **5.R.1 CRITICAL — WS endpoint was unauthenticated with no ownership
  check.** Anyone with a run id could stream another user's ingestion progress
  ("unguessable UUID" is not authorization). WebSockets cannot use the
  HTTPBearer dependency (browsers can't set Authorization on the WS upgrade),
  so `deps.get_ws_viewer` authenticates the handshake from either a `?token=`
  query param or the `access_token` httpOnly cookie — the browser connects
  through the Next.js BFF (ADR-008) and cookies flow on the same-origin WS
  handshake. It reuses the extracted `_viewer_from_token` (same code path as
  `get_current_viewer`) and scopes admins down like `get_scoped_viewer`.
  The endpoint accepts, authenticates, then enforces
  `run.owner_id == viewer.user_id` BEFORE streaming: missing/invalid token →
  close 1008 (policy violation); another user's run or an unknown id → the
  same generic close 4404 (invisible == nonexistent, get_run standard),
  re-checked every poll. RED first:
  `test_ws_progress_auth_required_and_ownership_enforced` + the updated
  happy-path test failed against the open endpoint. Test infra fallout fixed
  alongside: WS TestClient apps override `get_db` with a NullPool engine
  (asyncpg connections are loop-bound; the global engine's pool poisoned the
  second TestClient's fresh event loop) and `_ws_connect` tolerates the
  starlette teardown race on server-initiated closes (ClosedResourceError).

- [x] **5.R.2 IMPORTANT — undisclosed deviation from kb-celery-jobs rule 8,
  now documented (no code change; orchestrator-approved).** Rule 8 says worker
  progress reaches users via a `WsEvent` published to Redis pub/sub channel
  `run:{ingestion_run_id}` with the WS gateway relaying. Task 5 shipped DB
  polling (500 ms, `populate_existing=True`) without flagging the conflict.
  Decision: KEEP polling for now — the worker checkpoint-commits per item
  (4.R.1) so polling is correct, and it is one less moving part at
  single-team scale. The rule-8 pub/sub relay becomes the **Phase 7 hardening
  upgrade path**, alongside the MinIO payload offload (4.R.3): move the
  payload to MinIO and the progress events to `run:{id}` in the same pass.
  Documented in the WS handler docstring and here.

- [x] **5.R.3 IMPORTANT — zip-bomb guard in `md_importer.parse_zip`.** The
  100 MB upload cap bounds only the compressed payload; a high-ratio archive
  can declare gigabytes from kilobytes, and nothing capped member count.
  Added module-top constants `ZIP_MAX_MEMBERS = 5000` and
  `ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024` plus
  `check_zip_limits(zf)`, called by `parse_zip` before extracting anything —
  the DECLARED size sum is a real bound because ZipExtFile never reads past a
  header's `file_size`. Raises ValueError; the uploads endpoint runs the same
  guard at the door (ZipFile + check_zip_limits replaces `is_zipfile`) and
  maps BadZipFile/ValueError to 422; in the worker parse_zip's ValueError
  marks the run failed like any parse error (Task 4 failure path, already
  tested). RED first: `test_zip_bomb_declared_size_rejected`,
  `test_zip_too_many_members_rejected`, `test_zip_limit_constants_are_sane`
  (importer) and `test_upload_rejects_zip_bomb` (endpoint 422, nothing
  enqueued) all failed with AttributeError before the constants/guard existed.

- [x] **5.R.4 IMPORTANT — enqueue-then-crash leaves the run pending forever.**
  The run row commits durably BEFORE `ingest_md.delay`; if the broker publish
  then raises (Redis down), the client got a 500 and the row lied "pending"
  eternally — no worker was ever going to pick it up. Fixed: the enqueue is
  wrapped in try/except; on failure the run is marked
  `status=failed, error_log="enqueue failed: ...", finished_at=now` in a fresh
  commit and the endpoint answers 503 ("ingestion queue unavailable, try again
  later"). RED first: `test_upload_enqueue_failure_marks_run_failed_and_503`
  (monkeypatched `delay` raising) failed with the raw RuntimeError → 500
  before the guard existed.

---

## Task 6 — Service token API

> [plan-fix] vs the original blocks:
> - `get_scoped_viewer`, not `get_current_viewer` — the admin visibility bypass is
>   only reachable under `/api/v1/admin/*` (Phase 1 standard, Task 5 precedent).
> - Revoking another user's token answers a generic **404** (invisible ==
>   nonexistent, get_run standard), not the plan's 403 — a 403 confirms the token
>   id exists. `ForbiddenError` dropped.
> - `ApiToken.revoked == False` → `.is_(False)` (ruff E712).
> - Typed `viewer: Viewer`, return annotations, `summary`/`operation_id` per
>   kb-api-conventions; checklist tests (401, cross-user 404, 422) and
>   hashed-at-rest assertions added; `pytestmark` dropped (asyncio_mode="auto").

**Files:**
- Create: `backend/app/api/v1/tokens.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_tokens_api.py`

### Steps

- [x] **6.1** Write failing tests:

```python
# backend/tests/api/test_tokens_api.py
import uuid

from argon2 import PasswordHasher
from httpx import AsyncClient
from sqlalchemy import select

from app.models.ingest import ApiToken


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


async def test_create_token_stores_hash_not_plaintext(client: AsyncClient, auth_headers, db):
    r = await client.post(
        "/api/v1/tokens", json={"name": "hashed", "scopes": ["read"]}, headers=auth_headers
    )
    raw = r.json()["token"]
    row = await db.scalar(select(ApiToken).where(ApiToken.id == uuid.UUID(r.json()["id"])))
    assert row.token_hash != raw
    PasswordHasher().verify(row.token_hash, raw)  # raises on mismatch


async def test_list_tokens(client: AsyncClient, auth_headers):
    await client.post(
        "/api/v1/tokens", json={"name": "t1", "scopes": ["read"]}, headers=auth_headers
    )
    r = await client.get("/api/v1/tokens", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()) >= 1
    # plus: no "token"/"token_hash" keys in list items (see the test file)


async def test_revoke_token(client: AsyncClient, auth_headers):
    r = await client.post(
        "/api/v1/tokens", json={"name": "t2", "scopes": ["read"]}, headers=auth_headers
    )
    tid = r.json()["id"]
    r2 = await client.delete(f"/api/v1/tokens/{tid}", headers=auth_headers)
    assert r2.status_code == 204


# plus checklist tests (see backend/tests/api/test_tokens_api.py):
# 401 unauthenticated on all three endpoints; 422 missing name;
# cross-user list isolation; cross-user/missing revoke → generic 404.
```

- [x] **6.2** Create `tokens.py`:

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
from app.core.deps import Viewer, get_scoped_viewer
from app.core.errors import NotFoundError
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


@router.post(
    "",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a service token (raw token shown once)",
    operation_id="createToken",
)
async def create_token(
    payload: TokenCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> TokenCreated:
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


@router.get(
    "",
    response_model=list[TokenOut],
    summary="List my active service tokens",
    operation_id="listTokens",
)
async def list_tokens(
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> list[TokenOut]:
    rows = await db.scalars(
        select(ApiToken).where(ApiToken.owner_id == viewer.user_id, ApiToken.revoked.is_(False))
    )
    return [TokenOut.model_validate(row) for row in rows]


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a service token",
    operation_id="revokeToken",
)
async def revoke_token(
    token_id: uuid.UUID,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> None:
    token = await db.scalar(select(ApiToken).where(ApiToken.id == token_id))
    if token is None or token.owner_id != viewer.user_id:
        # Generic body: invisible == nonexistent, nothing confirmed either way.
        raise NotFoundError("Token not found")
    token.revoked = True
    await db.commit()
```

- [x] **6.3** Register in `main.py` ([plan-fix] `from … import router as …` — the
  file's established import style):
```python
from app.api.v1.tokens import router as tokens_router
app.include_router(tokens_router, prefix="/api/v1")
```

- [x] **6.4** Run all tests + full gate:
```bash
cd backend && pytest tests/ -v --tb=short
ruff check .
mypy --strict app/services/ app/schemas/
```

- [x] **6.5** Commit:
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
