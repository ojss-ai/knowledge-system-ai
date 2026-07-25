# Phase 2 — Search & Embeddings

**Goal:** Add semantic chunking, vector embeddings via Celery workers, and a hybrid full-text + vector search endpoint with RRF fusion. Auto-linking (SIMILAR_TO edges) runs as a post-embed task.

**Architecture refs:** ADR-003 (pgvector HNSW), ADR-007 (Celery+Redis), ADR-001 (single Postgres)

**Required skills (read before any task):**
- `kb-conventions`
- `kb-tdd-workflow`
- `kb-visibility-filter` — search legs apply visibility INSIDE each CTE
- `kb-pgvector-search` — exact RRF SQL, HNSW index DDL, auto-link algorithm
- `kb-celery-jobs` — task shape, idempotency requirement
- `kb-api-conventions`

**Exit criteria:**
- [x] All tasks checked
- [x] `pytest backend/tests/` green — `123 passed, 12 skipped` (skips = approved
  Neo4j-unreachable deviation; convert to passes on the Docker stack)
- [x] `ruff check backend/` + `ruff format --check` clean
- [x] `mypy --strict` clean across `app/api app/services app/schemas app/workers` (31 files)
- [x] `/kb-verify` passes — static visibility audit clean (all knowledge_nodes/node_chunks
  reads compose `visible_nodes_clause` or the documented SYSTEM_VIEWER; raw SQL legs filter
  inside CTEs pre-LIMIT); dynamic audit 25 passed; fresh-DB `alembic upgrade head` OK incl.
  HNSW index
- [x] Idempotency: `pytest -k "idempotent or stale or empty_body"` → 7 passed (embed twice →
  no dup chunks; empty re-embed clears stale chunks; autolink re-run replaces system edges)
- [x] `curl` evidence for `GET /api/v1/search?q=...` in §8.4 (200 + 401)

---

## Task 1 — node_chunks model + HNSW migration

**Files:**
- Create: `backend/app/models/chunk.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0004_node_chunks.py`
- Create: `backend/tests/models/test_chunk_model.py`

### Steps

> **[plan-fix] notes (applied during execution):**
> - Migration `down_revision` is `38ca9223b637` (actual phase-1 head), not `0003`.
> - Migration adds `CREATE EXTENSION IF NOT EXISTS vector` so it applies on a fresh DB.
> - No single-column `ix_node_chunks_node_id` (neither `index=True` nor `op.create_index`):
>   `uq_chunk_node_idx` leads with `node_id`, matching the phase-1 redundant-index removal.

- [x] **1.1** Write the failing test:

```python
# backend/tests/models/test_chunk_model.py
import uuid
import pytest
from sqlalchemy import select, text
from app.models.chunk import NodeChunk

pytestmark = pytest.mark.asyncio


async def test_chunk_create(db, make_user, make_node):
    owner = await make_user(email="chunk@test.com")
    node = await make_node(owner)
    await db.flush()
    chunk = NodeChunk(
        id=uuid.uuid4(),
        node_id=node.id,
        chunk_index=0,
        chunk_text="some text",
        embedding=[0.1] * 768,
    )
    db.add(chunk)
    await db.flush()
    result = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert result is not None
    assert len(result.embedding) == 768


async def test_hnsw_index_exists(db):
    result = await db.execute(
        text("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'node_chunks'
            AND indexdef ILIKE '%hnsw%'
        """)
    )
    rows = result.fetchall()
    assert len(rows) >= 1, "HNSW index on node_chunks.embedding must exist"
```

- [x] **1.2** Run — expect ImportError:
```bash
cd backend && pytest tests/models/test_chunk_model.py -x 2>&1 | head -20
```

- [x] **1.3** Create the model:

```python
# backend/app/models/chunk.py
from __future__ import annotations

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NodeChunk(Base):
    __tablename__ = "node_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No single-column index on node_id: uq_chunk_node_idx unique index leads with it.
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("node_id", "chunk_index", name="uq_chunk_node_idx"),)
```

- [x] **1.4** Add to `backend/app/models/__init__.py`:
```python
from app.models.chunk import NodeChunk  # noqa: F401
```

- [x] **1.5** Write the Alembic migration:

```python
# backend/alembic/versions/0004_node_chunks.py
"""node_chunks with pgvector HNSW index

Revision ID: 0004
Revises: 38ca9223b637
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
# [plan-fix] plan said down_revision "0003"; the actual head is 38ca9223b637 (knowledge_core).
down_revision: str | Sequence[str] | None = "38ca9223b637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [plan-fix] fresh databases need the extension before Vector columns / HNSW (ADR-003).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "node_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("node_id", "chunk_index", name="uq_chunk_node_idx"),
    )
    # [plan-fix] no single-column ix_node_chunks_node_id: uq_chunk_node_idx leads with node_id
    # (same redundant-index removal applied across phase-1 models).
    # HNSW index for cosine similarity (ADR-003)
    op.execute("""
        CREATE INDEX ix_node_chunks_embedding_hnsw
        ON node_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.drop_index("ix_node_chunks_embedding_hnsw")
    op.drop_table("node_chunks")
```

- [x] **1.6** Apply and test:
```bash
cd backend && alembic upgrade head
pytest tests/models/test_chunk_model.py -v
# Expected: 2 passed
```

- [x] **1.7** Commit:
```
feat(models): node_chunks model + migration 0004 with HNSW index (m=16, ef=64, cosine)
```

---

## Task 2 — Celery app setup

**Files:**
- Create: `backend/app/workers/__init__.py`
- Create: `backend/app/workers/celery_app.py`
- Create: `backend/tests/workers/__init__.py`
- Create: `backend/tests/workers/test_celery_app.py`
- Modify: `backend/pyproject.toml` — declare `celery>=5.4` dependency + mypy override for untyped `celery.*`

### Steps

> **[plan-fix] notes (applied during execution):**
> - Test 3 originally asserted on the `task_session` function object, but `@asynccontextmanager`
>   returns a plain wrapper (neither an asyncgen function nor owning `__aenter__`) — the plan's
>   own implementation failed it. The test now asserts on what `task_session()` returns,
>   matching its stated intent.
> - `from typing import AsyncIterator` → `collections.abc` (ruff UP035, matches `app/core/db.py`).
> - `celery` was not declared in `pyproject.toml`; added, with a `celery.*` mypy
>   `ignore_missing_imports` override (celery ships no py.typed marker).

- [x] **2.1** Write the failing test:

```python
# backend/tests/workers/test_celery_app.py
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
```

- [x] **2.2** Run — expect ImportError:
```bash
cd backend && pytest tests/workers/test_celery_app.py -x 2>&1 | head -10
```

- [x] **2.3** Implement:

```python
# backend/app/workers/celery_app.py
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
    """
    from app.core.db import SessionLocal

    async with SessionLocal() as session:
        async with session.begin():
            yield session
```

- [x] **2.4** Run tests:
```bash
cd backend && pytest tests/workers/test_celery_app.py -v
# Expected: 3 passed
```

- [x] **2.5** Commit:
```
feat(workers): Celery app setup with task_session context manager
```

---

## Task 3 — Chunking service

**Files:**
- Create: `backend/app/services/chunking.py`
- Create: `backend/tests/services/test_chunking.py`

### Steps

- [x] **3.1** Write the failing tests:

```python
# backend/tests/services/test_chunking.py
from app.services.chunking import chunk_markdown


def test_empty_body():
    chunks = chunk_markdown("", max_tokens=512)
    assert chunks == []


def test_short_body_is_single_chunk():
    body = "This is a short note."
    chunks = chunk_markdown(body, max_tokens=512)
    assert len(chunks) == 1
    assert chunks[0] == body


def test_heading_split():
    body = "# Section A\n\nContent A.\n\n# Section B\n\nContent B."
    chunks = chunk_markdown(body, max_tokens=512)
    assert len(chunks) == 2
    assert "Content A" in chunks[0]
    assert "Content B" in chunks[1]


def test_long_section_splits_by_tokens():
    # 600 words, each ~1.3 tokens ≈ 780 tokens — should split with max_tokens=512
    body = "word " * 600
    chunks = chunk_markdown(body, max_tokens=512)
    assert len(chunks) >= 2


def test_chunk_overlap():
    body = "word " * 600
    chunks = chunk_markdown(body, max_tokens=512, overlap_tokens=64)
    # With overlap, second chunk should share some words with first
    first_words = set(chunks[0].split()[-30:])
    second_words = set(chunks[1].split()[:30])
    assert len(first_words & second_words) > 0
```

- [x] **3.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_chunking.py -x 2>&1 | head -10
```

- [x] **3.3** Implement:

> [plan-fix] The original code block for `_split_by_tokens` infinite-looped when `overlap_tokens > 0`: once `end == len(text)`, `start = end - overlap_chars` stepped back and re-appended the tail chunk forever (pytest OOM-killed, exit 137, on `test_chunk_overlap`). Fixed by breaking out of the loop when `end >= len(text)`; the code block above is updated to match. Also synced a `ruff format` blank line after the module docstring.

```python
# backend/app/services/chunking.py
"""
Heading-aware Markdown chunker.

Strategy:
1. Split on heading lines (# ... through #### ...)
2. For sections that exceed max_tokens, further split by sentence boundary
3. Apply overlap_tokens sliding window between adjacent chunks
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^#{1,4}\s+", re.MULTILINE)
_APPROX_CHARS_PER_TOKEN = 4  # rough BPE estimate


def _token_count(text: str) -> int:
    return max(1, len(text) // _APPROX_CHARS_PER_TOKEN)


def _split_by_tokens(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a flat string into token-bounded chunks with overlap."""
    max_chars = max_tokens * _APPROX_CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _APPROX_CHARS_PER_TOKEN

    if len(text) <= max_chars:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        # Prefer breaking at sentence boundary
        if end < len(text):
            # Look backwards for '. ' or '\n'
            for sep in (". ", "\n", " "):
                idx = text.rfind(sep, start, end)
                if idx != -1 and idx > start:
                    end = idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap_chars if overlap_tokens > 0 else end

    return chunks


def chunk_markdown(
    body: str,
    max_tokens: int = 512,
    overlap_tokens: int = 0,
) -> list[str]:
    """
    Split `body` into a list of text chunks for embedding.
    Heading boundaries are preserved as natural split points.
    """
    if not body or not body.strip():
        return []

    # Find all heading positions
    heading_positions = [m.start() for m in _HEADING_RE.finditer(body)]

    if not heading_positions:
        return _split_by_tokens(body, max_tokens, overlap_tokens)

    # Split body at heading boundaries
    sections: list[str] = []
    for i, pos in enumerate(heading_positions):
        end = heading_positions[i + 1] if i + 1 < len(heading_positions) else len(body)
        sections.append(body[pos:end])

    # Pre-heading content (before first heading)
    if heading_positions[0] > 0:
        sections.insert(0, body[: heading_positions[0]])

    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if _token_count(section) <= max_tokens:
            chunks.append(section)
        else:
            chunks.extend(_split_by_tokens(section, max_tokens, overlap_tokens))

    return [c for c in chunks if c]
```

- [x] **3.4** Run tests:
```bash
cd backend && pytest tests/services/test_chunking.py -v
# Expected: 5 passed
```

- [x] **3.5** Commit:
```
feat(chunking): heading-aware Markdown chunker with token overlap
```

---

## Task 4 — Embedding service with protocol + FakeEmbedder

**Files:**
- Create: `backend/app/services/embedding_service.py`
- Create: `backend/tests/services/test_embedding_service.py`
- Modify: `backend/tests/conftest.py` (add `fake_embedder` fixture)

### Steps

> **[plan-fix] notes (applied during execution):**
> - Test file: dropped unused `import pytest` (ruff F401), import order normalised.
> - Service: annotations added for `mypy --strict` (`results`/`vec` locals, `self._model: Any`,
>   `_load(self) -> Any`); `get_embedder` reads `settings.embedding_backend` /
>   `settings.embedding_model` directly — the fields exist after 4.3, `getattr` defeats typing.
> - `pyproject.toml`: mypy override `sentence_transformers.*` → `ignore_missing_imports`
>   (package is integration-only and not installed; import stays lazy inside `_load`).

- [x] **4.1** Write the failing tests:

```python
# backend/tests/services/test_embedding_service.py
from app.services.embedding_service import Embedder, EmbeddingDimension, FakeEmbedder


def test_fake_embedder_is_embedder():
    e = FakeEmbedder()
    assert isinstance(e, Embedder)


def test_fake_embedder_dimension():
    e = FakeEmbedder()
    vecs = e.embed(["hello", "world"])
    assert len(vecs) == 2
    assert len(vecs[0]) == EmbeddingDimension
    assert len(vecs[1]) == EmbeddingDimension


def test_fake_embedder_deterministic():
    e = FakeEmbedder()
    v1 = e.embed(["test text"])
    v2 = e.embed(["test text"])
    assert v1 == v2


def test_embedder_protocol_satisfied():
    """Any class with embed(list[str]) -> list[list[float]] satisfies the protocol."""

    class MyEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 768 for _ in texts]

    assert isinstance(MyEmbedder(), Embedder)
```

- [x] **4.2** Implement:

```python
# backend/app/services/embedding_service.py
from __future__ import annotations

import hashlib
import math
from typing import Any, Protocol, runtime_checkable

EmbeddingDimension = 768


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per text."""
        ...


class FakeEmbedder:
    """
    Deterministic fake embedder for unit tests.
    Produces a unit-normalised 768-d vector derived from the SHA-256 of the text.
    Never use in production.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
            vec: list[float] = []
            for i in range(EmbeddingDimension):
                # Pseudo-random but deterministic value
                val = math.sin(seed + i * 2654435761)
                vec.append(val)
            # L2-normalise
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            results.append([v / norm for v in vec])
        return results


class SentenceTransformersEmbedder:
    """
    Production embedder using sentence-transformers.
    Loaded lazily to avoid import cost in tests.
    Model: all-MiniLM-L12-v2 (768d) — override via EMBEDDING_MODEL env var.
    """

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L12-v2") -> None:
        self._model_name = model_name
        self._model: Any = None  # lazy

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True, batch_size=64)
        return [v.tolist() for v in vecs]


def get_embedder() -> Embedder:
    """
    Factory: returns the configured embedder.
    Set EMBEDDING_BACKEND=fake for tests, sentence_transformers for local,
    or ollama for on-prem LLM (Phase 7).
    """
    from app.core.config import settings

    if settings.embedding_backend == "fake":
        return FakeEmbedder()
    return SentenceTransformersEmbedder(settings.embedding_model)
```

- [x] **4.3** Add `embedding_backend` to Settings:

```python
# backend/app/core/config.py  (add field)
embedding_backend: str = "sentence_transformers"  # fake | sentence_transformers | ollama
embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2"
```

- [x] **4.4** Add `fake_embedder` to conftest:

```python
# backend/tests/conftest.py (add)
from app.services.embedding_service import FakeEmbedder

@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
```

- [x] **4.5** Run tests:
```bash
cd backend && pytest tests/services/test_embedding_service.py -v
# Expected: 4 passed
```

- [x] **4.6** Commit:
```
feat(embedding): Embedder protocol, FakeEmbedder (deterministic), SentenceTransformersEmbedder
```

---

## Task 5 — embed_node Celery task (idempotent)

**Files:**
- Create: `backend/app/workers/tasks/embed_node.py`
- Create: `backend/tests/workers/test_embed_node.py`

### Steps

- [x] **5.1** Write the failing tests (idempotency test is MANDATORY per kb-celery-jobs):

> [plan-fix] dropped unused `uuid` / `FakeEmbedder` imports (ruff F401; the fixture provides the
> embedder). Added empty `backend/tests/workers/__init__.py` and `backend/app/workers/tasks/__init__.py`
> (test dirs are packages here, mirroring `tests/services/`).

```python
# backend/tests/workers/test_embed_node.py
import pytest
from sqlalchemy import func, select
from app.models.chunk import NodeChunk
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_embed_node_creates_chunks(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed1@test.com")
    node = await make_node(owner, body="# Section\n\nThis is content for embedding.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count >= 1


async def test_embed_node_idempotent(db, make_user, make_node, fake_embedder):
    """Running embed twice must NOT create duplicate chunks."""
    owner = await make_user(email="embed2@test.com")
    node = await make_node(owner, body="# A\n\nContent.\n\n# B\n\nMore content.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    count_after_first = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )

    await _embed_node_impl(db, node.id, fake_embedder)
    count_after_second = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )

    assert count_after_first == count_after_second, (
        "Re-running embed must not create duplicate chunks"
    )


async def test_embed_node_stores_vectors(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed3@test.com")
    node = await make_node(owner, body="Some text to embed.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    chunk = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert chunk.embedding is not None
    assert len(chunk.embedding) == 768
```

- [x] **5.2** Implement:

> [plan-fix] deviations from the original block, all lint/type driven, behavior identical:
> `asyncio.run(_run())` instead of `asyncio.get_event_loop().run_until_complete(...)`
> (deprecated on 3.12, breaks off the main thread); `zip(..., strict=True)` (ruff B905);
> `raise self.retry(exc=exc) from exc` (ruff B904); removed unused `shared_task` import and
> empty `TYPE_CHECKING` block; `self: Task` annotation + `type: ignore[untyped-decorator]`
> so `mypy --strict app/workers` passes (celery is untyped).

```python
# backend/app/workers/tasks/embed_node.py
from __future__ import annotations

import asyncio
import uuid

from celery import Task
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.group import GroupMember
from app.models.knowledge import KnowledgeNode
from app.models.user import User
from app.services.chunking import chunk_markdown
from app.services.embedding_service import Embedder, get_embedder
from app.services.visibility import SYSTEM_VIEWER, visible_nodes_clause
from app.workers.celery_app import celery_app, task_session
from app.workers.tasks.autolink_node import autolink_node

# Primitive args for chaining autolink_node.delay: (node_id, user_id, role, group_ids)
AutolinkArgs = tuple[str, str, str, list[str]]


async def _embed_node_impl(
    db: AsyncSession, node_id: uuid.UUID, embedder: Embedder
) -> KnowledgeNode | None:
    """
    Core logic extracted for unit-testability (no Celery dependency).
    Idempotent: deletes existing chunks for the node before reinserting.
    Returns the embedded node, or None when it is gone/soft-deleted.
    """
    # SYSTEM_VIEWER justification (kb-visibility-filter rule 1): embedding is a
    # system job that must (re)index any LIVE node regardless of owner. Going
    # through visible_nodes_clause keeps the single visibility choke point and
    # still excludes soft-deleted rows; the result is never shown to a user.
    node = await db.scalar(
        select(KnowledgeNode).where(
            KnowledgeNode.id == node_id,
            visible_nodes_clause(SYSTEM_VIEWER),
        )
    )
    if node is None:
        return None

    texts = chunk_markdown(node.body)

    # Idempotent replace: the delete ALWAYS runs, even when the new chunk list
    # is empty — a body edited down to nothing must clear its stale chunks.
    await db.execute(delete(NodeChunk).where(NodeChunk.node_id == node_id))

    if not texts:
        return node

    vectors = embedder.embed(texts)

    for idx, (text, vec) in enumerate(zip(texts, vectors, strict=True)):
        chunk = NodeChunk(
            node_id=node_id,
            chunk_index=idx,
            chunk_text=text,
            embedding=vec,
        )
        db.add(chunk)

    await db.flush()
    return node


async def _embed_and_prepare_autolink(
    db: AsyncSession, node_id: uuid.UUID, embedder: Embedder
) -> AutolinkArgs | None:
    """
    Embed the node, then build the primitive args for chaining autolink_node
    (plan Goal: auto-linking runs as a post-embed task). Runs INSIDE the task
    session; the actual .delay happens post-commit via _after_embed.

    The args are the node OWNER's viewer (user id, role, group ids): autolink's
    candidate reads must carry the owner's visibility, never SYSTEM_VIEWER.
    Returns None (no chaining) when the node or its owner is gone.
    """
    node = await _embed_node_impl(db, node_id, embedder)
    if node is None:
        return None

    owner_role = await db.scalar(select(User.role).where(User.id == node.owner_id))
    if owner_role is None:  # owner row gone (FK race): nothing to link on behalf of
        return None
    group_ids = await db.scalars(
        select(GroupMember.group_id).where(GroupMember.user_id == node.owner_id)
    )
    return (
        str(node.id),
        str(node.owner_id),
        owner_role.value,
        sorted(str(gid) for gid in group_ids),
    )


def _after_embed(autolink_args: AutolinkArgs | None) -> None:
    """
    Post-commit hook: chain autolink via the queue, never inline
    (kb-celery-jobs rule 7). Must be called AFTER task_session commits so the
    autolink worker sees the freshly written chunks.
    """
    if autolink_args is None:
        return
    autolink_node.delay(*autolink_args)


@celery_app.task(  # type: ignore[untyped-decorator]  # celery is untyped (ignore_missing_imports)
    bind=True,
    name="kb.embed_node",
    queue="embed",  # CPU/GPU-bound work (kb-celery-jobs rule 6); workers must consume -Q embed
    acks_late=True,
    max_retries=3,
    retry_backoff=True,
)
def embed_node(self: Task, node_id: str) -> None:
    """
    Celery task: chunk and embed a knowledge node, then chain autolink_node
    (post-embed task, kb-celery-jobs rule 7). Args must be primitives (str,
    not UUID).
    """
    nid = uuid.UUID(node_id)
    embedder = get_embedder()

    async def _run() -> AutolinkArgs | None:
        async with task_session() as db:
            return await _embed_and_prepare_autolink(db, nid, embedder)

    try:
        autolink_args = asyncio.run(_run())
    except Exception as exc:
        raise self.retry(exc=exc) from exc

    # Chain AFTER the session above committed: the autolink worker must see the
    # new chunks. Enqueue failures propagate and fail the task; embed_node is
    # idempotent, so a re-run (manual or requeued) is always safe.
    _after_embed(autolink_args)
```

- [x] **5.3** Run tests:
```bash
cd backend && pytest tests/workers/test_embed_node.py -v
# Expected: 3 passed (including idempotency test)
```

- [x] **5.4** Commit:
```
feat(workers): embed_node task — idempotent chunking + vector storage
```

### 5.R — Review fixes (post c6a7cc5, `/kb-review` findings)

> [plan-fix] The 5.2 code block above is kept in sync with these fixes.

- [x] **5.R.1 (CRITICAL)** `_embed_node_impl` read `knowledge_nodes` with a raw
  `select` + hand-rolled `deleted_at` check, violating kb-visibility-filter rule 1
  (system jobs use an explicit `SYSTEM_VIEWER`). Added module-level `SYSTEM_VIEWER`
  (admin role, all-zeros sentinel user id, justification docstring) to
  `app/services/visibility.py`; the task now reads via
  `visible_nodes_clause(SYSTEM_VIEWER)` with a use-site justification comment —
  soft-deleted rows stay excluded. Tests: `test_system_viewer_is_audited_admin_sentinel`,
  `test_system_viewer_sees_private_but_not_deleted` (test_visibility.py, RED via
  ImportError first) and `test_embed_node_skips_soft_deleted` (regression guard).

- [x] **5.R.2 (IMPORTANT)** Re-embedding a node whose body now chunks to nothing
  left stale chunks behind: the `if not texts: return` early-exit ran BEFORE the
  delete. The delete now always runs; the early return only skips the insert.
  Test (RED first): `test_reembed_empty_body_clears_stale_chunks` — embed, set
  `body=""`, re-embed, assert chunk count 0.

- [x] **5.R.3 (IMPORTANT)** Added `queue="embed"` to the task decorator per the
  kb-celery-jobs canonical shape (rule 6: embed queue for CPU/GPU work).
  **Operational note:** embedding workers must consume that queue —
  `celery -A app.workers.celery_app worker -Q embed`.

- [x] **5.R.4 (IMPORTANT)** Replaced `default_retry_delay=30` with
  `retry_backoff=True`, matching the skill template (`max_retries=3,
  retry_backoff=True, acks_late=True`). The skill does not prescribe
  `retry_jitter`, so it is not set. RED first for both via
  `test_embed_node_task_options` (asserts queue/backoff/acks_late/max_retries).

- [x] **5.R.5 (NIT)** Retry-path unit test: `test_embed_failure_propagates_for_retry`
  monkey-style BoomEmbedder raises; asserts `_embed_node_impl` propagates the
  exception (so the wrapper's `raise self.retry(exc=exc)` fires). Asserting
  `celery.exceptions.Retry` from the bound task directly requires a broker /
  task request context; per kb-celery-jobs that is integration territory —
  the design was not distorted for testability.

---

## Task 6 — autolink_node Celery task

**Files:**
- Create: `backend/app/workers/tasks/autolink_node.py`
- Create: `backend/tests/workers/test_autolink_node.py`
- Modify: `backend/app/services/graph_service.py` — `delete_autolink_edges` primitive (6.R.1)
- Modify: `backend/app/workers/tasks/embed_node.py` + tests — post-embed chaining (6.R.2)

### Steps

> **[plan-fix] notes (applied during execution):**
> - `graph_service` has no `create_vertex(db, node)` / `merge_edge(db, ..., props=...)`;
>   the real API (ADR-011) is `upsert_vertex(node)` and
>   `merge_edge(source_id, target_id, label, created_by, score=None)`. Code below uses it.
> - One `SIMILAR_TO` edge per pair, lower node id as source (kb-pgvector-search) —
>   not two directed edges as originally sketched.
> - The plan's raw `select(KnowledgeNode)` calls lacked `visible_nodes_clause`; every
>   knowledge_nodes read now carries the owner-viewer clause (kb-visibility-filter rule 1),
>   and the similarity query applies visibility INSIDE the query, before HAVING/LIMIT.
> - Task decorator mirrors `embed_node` (kb-celery-jobs): `queue="default"`,
>   `retry_backoff=True`, `acks_late=True`; wrapper uses `asyncio.run` (not the
>   deprecated `get_event_loop`).
> - The original tests verified edges via `gs.get_neighborhood` (needs live Neo4j).
>   They are kept as skip-when-unreachable tests; the pure-PG logic (similarity query,
>   threshold, top-K, candidate visibility) is covered with the graph-recorder
>   monkeypatch pattern from `tests/services/test_node_service.py` plus hand-crafted
>   chunk vectors (FakeEmbedder is degenerate for *different* texts — see report).

- [x] **6.1** Write the failing tests:

```python
# backend/tests/workers/test_autolink_node.py
import math
import uuid

import pytest

from app.models.chunk import NodeChunk
from app.models.user import Role, Visibility
from app.services.visibility import Viewer
from app.workers.tasks.autolink_node import _autolink_node_impl, autolink_node
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


# [plan-fix] The plan's two tests verified edges via gs.get_neighborhood, which
# needs live Neo4j; they are kept below (neo4j-marked). The pure-PG logic
# (similarity query, threshold, top-K, candidate visibility) is tested here with
# the graph-recorder pattern from tests/services/test_node_service.py so it runs
# without Neo4j.


def _graph_recorder(monkeypatch):
    """Patch graph_service functions with recorders; return the call log."""
    from app.services import graph_service as gs

    calls: list[tuple[str, ...]] = []

    async def fake_upsert(node):
        calls.append(("upsert", str(node.id)))

    async def fake_merge(source_id, target_id, label, created_by, score=None):
        calls.append(("edge", str(source_id), str(target_id), label, created_by))

    async def fake_delete_autolink(node_id):
        calls.append(("delete", str(node_id)))

    monkeypatch.setattr(gs, "upsert_vertex", fake_upsert)
    monkeypatch.setattr(gs, "merge_edge", fake_merge)
    monkeypatch.setattr(gs, "delete_autolink_edges", fake_delete_autolink)
    return calls


def _vec_with_cosine(cos_to_e1: float) -> list[float]:
    """Unit vector whose cosine to e1 = [1, 0, ...] is exactly cos_to_e1."""
    v = [0.0] * 768
    v[0] = cos_to_e1
    v[1] = math.sqrt(1.0 - cos_to_e1**2)
    return v


async def _add_chunk(db, node, vec: list[float]) -> None:
    db.add(
        NodeChunk(id=uuid.uuid4(), node_id=node.id, chunk_index=0, chunk_text="t", embedding=vec)
    )
    await db.flush()


def _viewer(user) -> Viewer:
    return Viewer(user_id=user.id, role=Role.user, group_ids=frozenset())


async def test_autolink_task_options():
    """Canonical kb-celery-jobs task shape: default queue, backoff retries, late acks."""
    assert autolink_node.queue == "default", "autolink is light DB/graph I/O -> default queue"
    assert autolink_node.retry_backoff is True
    assert autolink_node.acks_late is True
    assert autolink_node.max_retries == 3


async def test_autolink_creates_similar_to_edge(
    db, make_user, make_node, fake_embedder, monkeypatch
):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_rec1@test.com")
    # FakeEmbedder is deterministic — same text = identical vector = cosine 1.0
    n1 = await make_node(
        owner,
        title="Python Tips",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    n2 = await make_node(
        owner,
        title="Python Guide",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    await _autolink_node_impl(db, n1.id, _viewer(owner))

    # one SIMILAR_TO per pair, lower node id as source (kb-pgvector-search)
    src, tgt = sorted((n1.id, n2.id))
    assert ("edge", str(src), str(tgt), "SIMILAR_TO", "system:autolink") in calls


async def test_autolink_deletes_stale_edges_before_merging(
    db, make_user, make_node, fake_embedder, monkeypatch
):
    """Re-run replaces the node's previous auto edges (kb-pgvector-search):
    delete created_by='system:autolink' edges exactly once, BEFORE any MERGE."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_del@test.com")
    n1 = await make_node(
        owner, title="Del A", body="Shared delete-test body.", visibility=Visibility.public
    )
    n2 = await make_node(
        owner, title="Del B", body="Shared delete-test body.", visibility=Visibility.public
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    await _autolink_node_impl(db, n1.id, _viewer(owner))

    delete_idxs = [i for i, c in enumerate(calls) if c[0] == "delete"]
    merge_idxs = [i for i, c in enumerate(calls) if c[0] == "edge"]
    assert merge_idxs, "sanity: the run must merge a new edge"
    assert len(delete_idxs) == 1, "stale-edge delete must run exactly once per run"
    assert calls[delete_idxs[0]] == ("delete", str(n1.id)), "delete targets the re-run node"
    assert delete_idxs[0] < min(merge_idxs), "delete must run BEFORE any edge MERGE"


async def test_autolink_content_change_replaces_stale_edges(db, make_user, make_node, monkeypatch):
    """Content changed → different top-K set: the re-run deletes the node's old
    auto edges first, then merges ONLY the new set (old target never re-merged)."""
    from sqlalchemy import update

    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_chg@test.com")
    src = await make_node(owner, title="Src", visibility=Visibility.public)
    old = await make_node(owner, title="Old", visibility=Visibility.public)
    new = await make_node(owner, title="New", visibility=Visibility.public)
    await _add_chunk(db, src, _vec_with_cosine(1.0))
    await _add_chunk(db, old, _vec_with_cosine(0.9))  # similar to ORIGINAL content
    await _add_chunk(db, new, _vec_with_cosine(-0.2))  # cos(v(-0.2), v(0.9)) ≈ 0.25 < 0.82

    await _autolink_node_impl(db, src.id, _viewer(owner))
    s, t = sorted((src.id, old.id))
    assert [c for c in calls if c[0] == "edge"] == [
        ("edge", str(s), str(t), "SIMILAR_TO", "system:autolink")
    ], "sanity: first run links src to old"

    # "Content changed": src re-embedded to a vector matching `new`, far from `old`.
    await db.execute(
        update(NodeChunk)
        .where(NodeChunk.node_id == src.id)
        .values(embedding=_vec_with_cosine(-0.2))
    )
    await db.flush()

    calls.clear()
    await _autolink_node_impl(db, src.id, _viewer(owner))

    assert calls[0] == ("delete", str(src.id)), "re-run must delete stale auto edges first"
    edges = [c for c in calls if c[0] == "edge"]
    s, t = sorted((src.id, new.id))
    assert edges == [("edge", str(s), str(t), "SIMILAR_TO", "system:autolink")], (
        "only the NEW top-K set is merged; the old target must be gone"
    )


async def test_autolink_idempotent(db, make_user, make_node, fake_embedder, monkeypatch):
    """Re-running autolink issues the exact same calls (delete + MERGEs) — no
    new/extra edges."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_rec2@test.com")
    n1 = await make_node(
        owner, title="Topic A", body="Same content here.", visibility=Visibility.public
    )
    n2 = await make_node(
        owner, title="Topic B", body="Same content here.", visibility=Visibility.public
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)
    first_run = list(calls)
    edges = [c for c in first_run if c[0] == "edge"]
    assert len(edges) == len(set(edges)) == 1, "exactly one MERGE per pair, once"

    calls.clear()
    await _autolink_node_impl(db, n1.id, viewer)  # second run
    assert calls == first_run, "second run must repeat identical MERGEs (idempotent)"


async def test_autolink_respects_cosine_threshold(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_thr@test.com")
    src = await make_node(owner, title="Src", visibility=Visibility.public)
    near = await make_node(owner, title="Near", visibility=Visibility.public)
    far = await make_node(owner, title="Far", visibility=Visibility.public)
    await _add_chunk(db, src, _vec_with_cosine(1.0))
    await _add_chunk(db, near, _vec_with_cosine(0.9))  # >= 0.82 -> linked
    await _add_chunk(db, far, _vec_with_cosine(0.5))  # < 0.82 -> not linked

    await _autolink_node_impl(db, src.id, _viewer(owner))

    linked = {c[2] for c in calls if c[0] == "edge"} | {c[1] for c in calls if c[0] == "edge"}
    assert str(near.id) in linked
    assert str(far.id) not in linked


async def test_autolink_caps_at_top_k(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_topk@test.com")
    src = await make_node(owner, title="Src", visibility=Visibility.public)
    await _add_chunk(db, src, _vec_with_cosine(1.0))

    cosines = [0.99, 0.97, 0.95, 0.93, 0.91, 0.89, 0.85]  # 7 candidates over threshold
    nodes = []
    for i, c in enumerate(cosines):
        n = await make_node(owner, title=f"Cand {i}", visibility=Visibility.public)
        await _add_chunk(db, n, _vec_with_cosine(c))
        nodes.append(n)

    await _autolink_node_impl(db, src.id, _viewer(owner))

    edges = [c for c in calls if c[0] == "edge"]
    assert len(edges) == 5, "top-K is 5"
    linked = {e[1] for e in edges} | {e[2] for e in edges}
    for n in nodes[:5]:
        assert str(n.id) in linked, "the 5 MOST similar candidates must win"
    for n in nodes[5:]:
        assert str(n.id) not in linked


async def test_autolink_excludes_other_users_private_nodes(
    db, make_user, make_node, fake_embedder, monkeypatch
):
    """Candidate set uses the OWNER's viewer: another user's private node must
    never be auto-linked, even at cosine 1.0 (existence must not leak)."""
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_vis1@test.com")
    other = await make_user(email="al_vis2@test.com")
    mine = await make_node(
        owner, title="Mine", body="identical secret content", visibility=Visibility.public
    )
    secret = await make_node(
        other, title="Secret", body="identical secret content", visibility=Visibility.private
    )
    await db.flush()

    await _embed_node_impl(db, mine.id, fake_embedder)
    await _embed_node_impl(db, secret.id, fake_embedder)

    await _autolink_node_impl(db, mine.id, _viewer(owner))

    assert all(str(secret.id) not in c for c in calls), "private node leaked into autolink"


async def test_autolink_without_chunks_is_noop(db, make_user, make_node, monkeypatch):
    calls = _graph_recorder(monkeypatch)
    owner = await make_user(email="al_noop@test.com")
    node = await make_node(owner, title="Unembedded", visibility=Visibility.public)
    await db.flush()

    await _autolink_node_impl(db, node.id, _viewer(owner))
    assert calls == []


# --- Plan's original graph-level tests: need live Neo4j (skip when unreachable) ---


async def test_autolink_creates_similar_to_edges(
    db, neo4j_session, make_user, make_node, fake_embedder
):
    from app.services import graph_service as gs

    owner = await make_user(email="al1@test.com")
    n1 = await make_node(
        owner,
        title="Python Tips",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    n2 = await make_node(
        owner,
        title="Python Guide",
        body="Python is great for data science.",
        visibility=Visibility.public,
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    edge_labels = [e["label"] for e in hood["edges"]]
    assert "SIMILAR_TO" in edge_labels


async def test_autolink_idempotent_in_graph(db, neo4j_session, make_user, make_node, fake_embedder):
    """Running autolink twice must not create duplicate SIMILAR_TO edges."""
    from app.services import graph_service as gs

    owner = await make_user(email="al2@test.com")
    n1 = await make_node(
        owner, title="Topic A", body="Same content here.", visibility=Visibility.public
    )
    n2 = await make_node(
        owner, title="Topic B", body="Same content here.", visibility=Visibility.public
    )
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)
    await _autolink_node_impl(db, n1.id, viewer)  # second run

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    similar_edges = [e for e in hood["edges"] if e["label"] == "SIMILAR_TO"]
    targets = [(e["source"], e["target"]) for e in similar_edges]
    assert len(targets) == len(set(targets)), "Duplicate SIMILAR_TO edges detected"


async def test_autolink_rerun_removes_stale_edge_in_graph(
    db, neo4j_session, make_user, make_node, fake_embedder
):
    """Content changed → the old system:autolink SIMILAR_TO edge is gone from the
    graph after a re-run, while a MANUAL SIMILAR_TO edge on the same node survives
    (the delete filters on created_by='system:autolink')."""
    from app.services import graph_service as gs

    owner = await make_user(email="al3@test.com")
    n1 = await make_node(
        owner, title="Drift", body="Original shared content.", visibility=Visibility.public
    )
    n2 = await make_node(
        owner, title="Anchor", body="Original shared content.", visibility=Visibility.public
    )
    manual = await make_node(owner, title="Manual", visibility=Visibility.public)
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = _viewer(owner)
    await _autolink_node_impl(db, n1.id, viewer)

    # A user-created SIMILAR_TO edge on the same node must survive re-runs.
    await gs.upsert_vertex(manual)
    await gs.merge_edge(n1.id, manual.id, "SIMILAR_TO", created_by=str(owner.id))

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    pairs = {(e["source"], e["target"]) for e in hood["edges"] if e["label"] == "SIMILAR_TO"}
    assert (str(min(n1.id, n2.id)), str(max(n1.id, n2.id))) in pairs, "sanity: auto edge created"

    # Content changed: FakeEmbedder vectors for unrelated texts are ~orthogonal in 768-d.
    n1.body = "Completely different topic now: gardening, soil, and compost."
    await db.flush()
    await _embed_node_impl(db, n1.id, fake_embedder)
    await _autolink_node_impl(db, n1.id, viewer)

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    pairs = {(e["source"], e["target"]) for e in hood["edges"] if e["label"] == "SIMILAR_TO"}
    assert (str(min(n1.id, n2.id)), str(max(n1.id, n2.id))) not in pairs, (
        "stale system:autolink edge must be deleted on re-run"
    )
    assert (str(n1.id), str(manual.id)) in pairs, "manual SIMILAR_TO edge must survive"
```

- [x] **6.2** Implement:

```python
# backend/app/workers/tasks/autolink_node.py
from __future__ import annotations

import asyncio
import uuid

from celery import Task
from pgvector.sqlalchemy import Vector
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.models.user import Role
from app.services import graph_service as gs
from app.services.visibility import Viewer, visible_nodes_clause
from app.workers.celery_app import celery_app, task_session

_COSINE_THRESHOLD = 0.82
_TOP_K = 5


async def _autolink_node_impl(db: AsyncSession, node_id: uuid.UUID, viewer: Viewer) -> None:
    """
    Core logic extracted for unit-testability (no Celery dependency).

    Mean-pool the node's chunk vectors, find the top-K visible nodes whose best
    chunk has cosine >= threshold, and MERGE one SIMILAR_TO edge per pair with
    the lower node id as source (kb-pgvector-search). A re-run REPLACES the
    node's previous auto edges: stale created_by='system:autolink' edges are
    deleted before the new set is merged, so content drift never leaves ghost
    links. MERGE keeps the surviving set duplicate-free; there is no PG-side
    bookkeeping.

    `viewer` is the node OWNER's identity: a private node may auto-link only to
    nodes its owner can see (never SYSTEM_VIEWER — results become user-visible
    edges, so candidate reads must carry the owner's visibility).
    """
    source_node = await db.scalar(
        select(KnowledgeNode).where(KnowledgeNode.id == node_id, visible_nodes_clause(viewer))
    )
    if source_node is None:  # deleted or no longer visible: tolerate races
        return

    mean_vec = await db.scalar(
        select(func.avg(NodeChunk.embedding, type_=Vector(768))).where(
            NodeChunk.node_id == node_id, NodeChunk.embedding.is_not(None)
        )
    )
    if mean_vec is None:  # node has no embedded chunks yet
        return

    # Re-run replaces the node's previous auto edges (kb-pgvector-search): drop
    # stale system:autolink edges BEFORE merging, and before the early return
    # below — content drift may leave an empty candidate set, and the old edges
    # must still disappear. Manual SIMILAR_TO edges survive (created_by filter).
    await gs.delete_autolink_edges(node_id)

    # Best chunk per candidate node: MIN cosine distance == MAX cosine similarity.
    # Visibility applied INSIDE the query, before HAVING/LIMIT (kb-visibility-filter).
    distance = NodeChunk.embedding.cosine_distance(mean_vec)
    best_dist = func.min(distance)
    rows = (
        await db.execute(
            select(NodeChunk.node_id, (1 - best_dist).label("score"))
            .join(KnowledgeNode, KnowledgeNode.id == NodeChunk.node_id)
            .where(
                visible_nodes_clause(viewer),
                NodeChunk.node_id != node_id,
                NodeChunk.embedding.is_not(None),
            )
            .group_by(NodeChunk.node_id)
            .having(best_dist <= 1.0 - _COSINE_THRESHOLD)
            # node_id tiebreak keeps the top-K deterministic across re-runs
            .order_by(best_dist, NodeChunk.node_id)
            .limit(_TOP_K)
        )
    ).all()
    if not rows:
        return

    scores: dict[uuid.UUID, float] = {row.node_id: float(row.score) for row in rows}
    targets = (
        await db.scalars(
            select(KnowledgeNode).where(KnowledgeNode.id.in_(scores), visible_nodes_clause(viewer))
        )
    ).all()

    # Vertices must exist before MERGE edge Cypher can MATCH them.
    await gs.upsert_vertex(source_node)
    for target in targets:
        await gs.upsert_vertex(target)
        src_id, tgt_id = sorted((node_id, target.id))  # one edge per pair, lower id as source
        await gs.merge_edge(
            src_id,
            tgt_id,
            "SIMILAR_TO",
            created_by="system:autolink",
            score=scores[target.id],
        )


@celery_app.task(  # type: ignore[untyped-decorator]  # celery is untyped (ignore_missing_imports)
    bind=True,
    name="kb.autolink_node",
    queue="default",  # light DB/graph I/O, not CPU-bound (kb-celery-jobs rule 6)
    acks_late=True,
    max_retries=3,
    retry_backoff=True,
)
def autolink_node(self: Task, node_id: str, user_id: str, role: str, group_ids: list[str]) -> None:
    """
    Celery task: create SIMILAR_TO edges for a node after (re)embedding.
    Args must be primitives; (user_id, role, group_ids) is the node owner's viewer.
    """
    viewer = Viewer(
        user_id=uuid.UUID(user_id),
        role=Role(role),
        group_ids=frozenset(uuid.UUID(gid) for gid in group_ids),
    )
    nid = uuid.UUID(node_id)

    async def _run() -> None:
        async with task_session() as db:
            await _autolink_node_impl(db, nid, viewer)

    try:
        asyncio.run(_run())
    except Exception as exc:
        raise self.retry(exc=exc) from exc
```

- [x] **6.3** Run tests:
```bash
cd backend && pytest tests/workers/test_autolink_node.py -v
# Expected: 9 passed, 3 skipped (the live-graph tests skip when Neo4j is unreachable)
```

- [x] **6.4** Commit:
```
feat(workers): autolink_node task — mean-pool similarity, MERGE SIMILAR_TO edges, idempotent
```

### 6.R — Review fixes (post 276cd57, `/kb-review` findings)

> [plan-fix] The 6.1/6.2 code blocks above are kept in sync with these fixes.
> **Disclosure:** the original Task 6 commit (276cd57) silently dropped a prescribed step —
> kb-pgvector-search line ~46 says a re-run "replaces the node's previous auto edges
> (delete `created_by='system:autolink'` edges for the node first)", but the implementation
> only MERGEd and relied on MERGE idempotency, so edges from superseded content were never
> removed. The 6.2 [plan-fix] notes above did not mention that omission; this section
> records it plainly.

- [x] **6.R.1 (CRITICAL)** Stale autolink edges were never removed: after a node's
  content changed, autolink re-ran and MERGEd the new top-K set but left the old
  `SIMILAR_TO` edges in the graph forever. Added a `graph_service` primitive
  `delete_autolink_edges(node_id)` (Cypher:
  `MATCH (n:Node {node_id: $node_id})-[r:SIMILAR_TO {created_by: 'system:autolink'}]-() DELETE r`
  — undirected, since the lower-id-as-source convention can put the node at either
  end; the label + `created_by` filters keep manual `SIMILAR_TO` edges intact).
  `_autolink_node_impl` calls it after the mean-vector check and BEFORE the
  candidate query / early return, so stale edges disappear even when the new
  candidate set is empty. Tests (RED first via `AttributeError` on the recorder
  patching `gs.delete_autolink_edges`):
  `test_autolink_deletes_stale_edges_before_merging` (order-sensitive: exactly one
  delete, targeting the re-run node, before every MERGE),
  `test_autolink_content_change_replaces_stale_edges` (re-run with a different
  top-K set: delete first, then ONLY the new edge merged),
  `test_autolink_idempotent` updated (recorder now logs deletes; both runs must
  issue identical delete+MERGE sequences), and live-Neo4j
  `test_autolink_rerun_removes_stale_edge_in_graph` (neo4j_session skip pattern:
  stale auto edge gone after content change; a manual `SIMILAR_TO` edge on the
  same node survives). The no-chunks noop path intentionally stays a noop
  (returns before the delete), preserving `test_autolink_without_chunks_is_noop`.

- [x] **6.R.2 (IMPORTANT)** Autolink was never invoked: the plan Goal says
  auto-linking "runs as a post-embed task", but nothing enqueued `kb.autolink_node`.
  Neither Task 5 nor Task 6 prescribed the chaining mechanism, so the
  kb-celery-jobs canonical shape is used ([plan-fix]): the `embed_node` wrapper
  chains `autolink_node.delay(...)` AFTER `task_session` commits (rule 7: chain
  via the queue, never inline; post-commit so the autolink worker sees the new
  chunks). The chain is split into two broker-free testable pieces in
  `embed_node.py`: `_embed_and_prepare_autolink` (in-session; embeds, then builds
  the primitive args `(node_id, user_id, role, group_ids)` from the node OWNER's
  role + group memberships — autolink must use the owner's viewer, never
  SYSTEM_VIEWER) and `_after_embed` (post-commit hook calling `.delay`; None =
  embed skipped, no enqueue). `_embed_node_impl` now returns the node (or None).
  Tests (RED first via ImportError):
  `test_embed_prepare_chain_returns_owner_viewer_args` (owner in a group; exact
  arg tuple), `test_embed_prepare_chain_skips_missing_node`,
  `test_after_embed_enqueues_autolink` (monkeypatched `autolink_node.delay`
  recorder), `test_after_embed_noop_when_embed_skipped`. The 5.2 code block is
  kept in sync. **Docker-stack note:** end-to-end chaining (embed worker →
  broker → autolink on `default` queue) needs the real stack; verify with
  workers consuming both `-Q embed` and `-Q default`.

---

## Task 7 — Hybrid search service

> **Review notes (7.R):** (1) plainto_tsquery + kn.id tiebreaker fixes below are
> review CRITICALs (special-char 500s; nondeterministic OFFSET on ties). (2) Known
> divergence: the visible-id set is materialized in Python and bound as uuid[]
> instead of composing visible_nodes_clause inline per CTE — correct (filter still
> pre-LIMIT in both legs) but a scaling concern for very large visible sets;
> revisit if admin/org-wide search grows. (3) The FULL OUTER JOIN fusion is
> mathematically equivalent to the skill's UNION ALL/GROUP BY shape.

**Files:**
- Create: `backend/app/services/search_service.py`
- Create: `backend/tests/services/test_search_service.py`

### Steps

> **[plan-fix] notes (applied during execution):**
> - SQLAlchemy `text()` never recognises a bind followed by a `::` cast (its regex has a
>   `(?!:)` lookahead), so `:visible_ids::uuid[]` / `:query_vec::vector` reached Postgres
>   verbatim and raised `syntax error at or near ":"`. Rewritten as
>   `CAST(:param AS type)` in both queries.
> - Privacy test strengthened per kb-visibility-filter's mandatory shape: a public decoy
>   node matching the same query ensures the viewer's visible set is non-empty so BOTH
>   CTE legs actually execute (no empty-set early return), plus an existence AND content
>   assertion. Original version only checked existence against an empty result.
> - Dropped the unused `uuid` import and hoisted function-level imports to module top
>   (ruff); test blocks below reflect ruff-format output.

- [x] **7.1** Write the failing tests:

```python
# backend/tests/services/test_search_service.py
import pytest

from app.models.user import Role, Visibility
from app.services import search_service as ss
from app.services.embedding_service import FakeEmbedder
from app.services.visibility import Viewer
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_fts_finds_node(db, make_user, make_node):
    owner = await make_user(email="srch_fts@test.com")
    node = await make_node(
        owner,
        title="PostgreSQL Tips",
        body="Full-text search is powerful.",
        visibility=Visibility.public,
    )
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    results, total = await ss.hybrid_search(
        db, "PostgreSQL", viewer, embedder_override=FakeEmbedder()
    )
    ids = [r["id"] for r in results]
    assert str(node.id) in ids


async def test_vector_finds_similar(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="srch_vec@test.com")
    node = await make_node(
        owner,
        title="Vector Node",
        body="embeddings and similarity search",
        visibility=Visibility.public,
    )
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    results, total = await ss.hybrid_search(
        db, "embeddings similarity", viewer, embedder_override=fake_embedder
    )
    ids = [r["id"] for r in results]
    assert str(node.id) in ids


async def test_private_node_excluded_from_search(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="srch_priv@test.com")
    other = await make_user(email="srch_priv2@test.com")
    node = await make_node(
        owner, title="Secret", body="private secret content", visibility=Visibility.private
    )
    # [plan-fix] public decoy matching the same query: guarantees the viewer's
    # visible set is non-empty so BOTH CTE legs actually execute (no early
    # return), and serves as a positive control.
    decoy = await make_node(
        owner,
        title="Public Notes",
        body="public secret private discussion",
        visibility=Visibility.public,
    )
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)
    await _embed_node_impl(db, decoy.id, fake_embedder)

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    results, _ = await ss.hybrid_search(
        db, "secret private", viewer, embedder_override=fake_embedder
    )
    ids = [r["id"] for r in results]
    assert str(decoy.id) in ids, "sanity: both search legs ran for this viewer"
    assert str(node.id) not in ids, "Private node must not appear in another user's search"
    # kb-visibility-filter mandatory check: existence AND content
    assert not any("Secret" in r["title"] for r in results)
```

- [x] **7.2** Implement `search_service.py` with exact RRF fusion from kb-pgvector-search skill: *([phase-7 2.R.3]: the `fake_embedder` kwarg was renamed `embedder_override` once rag_service started injecting an embedder on a production path — snippets below updated to match)*

```python
# backend/app/services/search_service.py
"""
Hybrid full-text + vector search with RRF fusion.

CRITICAL invariants (ADR-004 / kb-visibility-filter):
- Visibility filter applied INSIDE each CTE leg, before LIMIT
- Never post-filter after merge — that would allow LIMIT to cut visible results
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode
from app.services.embedding_service import Embedder, get_embedder
from app.services.visibility import Viewer, visible_nodes_clause

_RRF_K = 60
_DEFAULT_LIMIT = 20
_EF_SEARCH = 80


async def hybrid_search(
    db: AsyncSession,
    query: str,
    viewer: Viewer,
    *,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
    embedder_override: Embedder | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Hybrid RRF search: FTS leg + vector leg fused with Reciprocal Rank Fusion.
    Returns (results, total_count).

    score = Σ 1 / (k + rank_i)   where k=60

    embedder_override injects an already-constructed Embedder — a production
    path (rag_service passes its embedder through), not a test-only hook.
    Defaults to get_embedder().
    """
    embedder = embedder_override or get_embedder()
    query_vec = embedder.embed([query])[0]

    # Build visibility predicate as a subquery node_id list
    clause = visible_nodes_clause(viewer)
    visible_ids_result = await db.scalars(select(KnowledgeNode.id).where(clause))
    visible_ids = [str(i) for i in visible_ids_result]

    if not visible_ids:
        return [], 0

    # Set ef_search for this session (HNSW quality vs speed)
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {_EF_SEARCH}"))

    vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"

    sql = text("""
        WITH visible AS (
            SELECT id FROM knowledge_nodes
            WHERE id = ANY(CAST(:visible_ids AS uuid[]))
        ),
        fts_ranked AS (
            SELECT kn.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(kn.body_tsv, query) DESC) AS rank
            FROM knowledge_nodes kn,
                 plainto_tsquery('english', :tsquery) AS query
            WHERE kn.id IN (SELECT id FROM visible)
              AND kn.body_tsv @@ query
            LIMIT 100
        ),
        vec_ranked AS (
            SELECT DISTINCT ON (nc.node_id) nc.node_id AS id,
                   ROW_NUMBER() OVER (ORDER BY nc.embedding <=> CAST(:query_vec AS vector)) AS rank
            FROM node_chunks nc
            WHERE nc.node_id IN (SELECT id FROM visible)
              AND nc.embedding IS NOT NULL
            ORDER BY nc.node_id, nc.embedding <=> CAST(:query_vec AS vector)
            LIMIT 100
        ),
        rrf AS (
            SELECT id,
                   COALESCE(fts.rrf_score, 0) + COALESCE(vec.rrf_score, 0) AS score
            FROM (
                SELECT id, 1.0 / (:k + rank) AS rrf_score FROM fts_ranked
            ) fts
            FULL OUTER JOIN (
                SELECT id, 1.0 / (:k + rank) AS rrf_score FROM vec_ranked
            ) vec USING (id)
        )
        SELECT kn.id, kn.title, kn.node_type, kn.visibility, kn.updated_at,
               rrf.score
        FROM rrf
        JOIN knowledge_nodes kn ON kn.id = rrf.id
        ORDER BY rrf.score DESC, kn.id
        LIMIT :limit OFFSET :offset
    """)

    count_sql = text("""
        WITH visible AS (
            SELECT id FROM knowledge_nodes WHERE id = ANY(CAST(:visible_ids AS uuid[]))
        ),
        fts_ids AS (
            SELECT kn.id FROM knowledge_nodes kn, plainto_tsquery('english', :tsquery) AS q
            WHERE kn.id IN (SELECT id FROM visible) AND kn.body_tsv @@ q
        ),
        vec_ids AS (
            SELECT DISTINCT nc.node_id AS id FROM node_chunks nc
            WHERE nc.node_id IN (SELECT id FROM visible) AND nc.embedding IS NOT NULL
        )
        SELECT COUNT(DISTINCT id) FROM (SELECT id FROM fts_ids UNION SELECT id FROM vec_ids) sub
    """)

    # Convert query to tsquery (simple: replace spaces with & for AND logic)
    # [plan-fix] review CRITICAL: hand-built to_tsquery strings crash on
    # parens/quotes ("issue(#123)" -> tsquery syntax error -> 500). The raw
    # query is passed to plainto_tsquery (implicit AND, safe parsing), per
    # kb-pgvector-search. Also: ", kn.id" tiebreaker on the final ORDER BY
    # keeps OFFSET pagination deterministic when RRF scores tie.
    tsquery = query

    params = {
        "visible_ids": visible_ids,
        "tsquery": tsquery,
        "query_vec": vec_str,
        "k": _RRF_K,
        "limit": limit,
        "offset": offset,
    }

    rows = (await db.execute(sql, params)).fetchall()
    total = (await db.scalar(count_sql, {**params})) or 0

    results = [
        {
            "id": str(row[0]),
            "title": row[1],
            "node_type": row[2],
            "visibility": row[3],
            "updated_at": row[4].isoformat() if row[4] else None,
            "score": float(row[5]) if row[5] else 0.0,
        }
        for row in rows
    ]

    return results, int(total)
```

- [x] **7.3** Run tests:
```bash
cd backend && pytest tests/services/test_search_service.py -v
# Expected: 3 passed
```

- [x] **7.4** Commit:
```
feat(search): hybrid RRF search (FTS + pgvector) with visibility inside each leg
```

---

## Task 8 — Search API endpoint

**Files:**
- Create: `backend/app/api/v1/search.py`
- Create: `backend/app/schemas/search.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/conftest.py`
- Create: `backend/tests/api/test_search_api.py`

### Steps

> **[plan-fix] notes (applied during execution):**
> - Viewer dep is `get_scoped_viewer`, not `get_current_viewer`: the admin
>   visibility bypass is only reachable under `/api/v1/admin/*`
>   (kb-visibility-filter rule 5, established in Phase 1 routers).
> - Response schemas moved from the router into `app/schemas/search.py`
>   (kb-api-conventions: schemas live in `app/schemas/`; mypy strict gate
>   covers that package).
> - Route carries `summary` + `operation_id="searchNodes"` (OpenAPI discipline).
> - Test infra: autouse fixture in `tests/conftest.py` forces
>   `settings.embedding_backend = "fake"` — the API path calls `get_embedder()`
>   at request time and must never lazy-load the real sentence-transformers
>   model in unit tests (kb-tdd-workflow: real model is integration-only).
> - `items` are validated explicitly via `SearchResultItem.model_validate(r)`
>   (mypy strict rejects passing `list[dict]` where `list[SearchResultItem]`
>   is expected).

- [x] **8.1** Write failing tests:

```python
# backend/tests/api/test_search_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_search_returns_results(client: AsyncClient, auth_headers):
    # Create a public node with searchable content
    await client.post("/api/v1/nodes", json={
        "title": "FastAPI Guide",
        "body": "FastAPI is a modern Python web framework for building APIs.",
        "visibility": "public",
    }, headers=auth_headers)

    r = await client.get("/api/v1/search?q=FastAPI", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "total" in data
    titles = [item["title"] for item in data["items"]]
    assert any("FastAPI" in t for t in titles)


async def test_search_excludes_private(client: AsyncClient, auth_headers, auth_headers_other):
    await client.post("/api/v1/nodes", json={
        "title": "Secret FastAPI Note",
        "body": "top secret fastapi content",
        "visibility": "private",
    }, headers=auth_headers)

    r = await client.get("/api/v1/search?q=secret+fastapi", headers=auth_headers_other)
    assert r.status_code == 200
    titles = [item["title"] for item in r.json()["items"]]
    assert not any("Secret" in t for t in titles)


async def test_search_requires_auth(client: AsyncClient):
    r = await client.get("/api/v1/search?q=test")
    assert r.status_code == 401
```

- [x] **8.2** Create the schemas and the router:

```python
# backend/app/schemas/search.py
from pydantic import BaseModel


class SearchResultItem(BaseModel):
    id: str
    title: str
    node_type: str
    visibility: str
    updated_at: str | None
    score: float


class SearchOut(BaseModel):
    items: list[SearchResultItem]
    total: int
    query: str
```

```python
# backend/app/api/v1/search.py
"""Search router — thin translation layer over search_service (ADR-005).

Visibility is enforced inside each search leg by hybrid_search
(kb-visibility-filter rule 3); the router never touches the tables.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer
from app.schemas.search import SearchOut, SearchResultItem
from app.services import search_service as ss

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchOut, summary="Hybrid search", operation_id="searchNodes")
async def search(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> SearchOut:
    results, total = await ss.hybrid_search(db, q, viewer, limit=limit, offset=offset)
    return SearchOut(
        items=[SearchResultItem.model_validate(r) for r in results],
        total=total,
        query=q,
    )
```

- [x] **8.3** Register in `main.py` (existing import style):

```python
from app.api.v1.search import router as search_router
app.include_router(search_router, prefix="/api/v1")
```

- [x] **8.4** Run all tests + curl evidence:
```bash
cd backend && pytest tests/ -v --tb=short
# Expected: all pass

curl -s "http://localhost:8000/api/v1/search?q=FastAPI" \
  -H "Authorization: Bearer $TOKEN" | jq '{total: .total, first: .items[0].title}'
```

Evidence (sandbox: pgserver PG16+pgvector; Neo4j tests auto-skip;
`EMBEDDING_BACKEND=fake` exported for the live server — real model is
integration-only; evidence user/node rows deleted afterwards):

```text
$ pytest tests/ -q --tb=short
123 passed, 12 skipped in 15.34s

$ curl -s "http://127.0.0.1:8000/api/v1/search?q=FastAPI" -H "Authorization: Bearer $TOKEN"
{
    "items": [
        {
            "id": "3923338e-9311-4260-8f06-0a5d3db9bdfe",
            "title": "FastAPI Guide",
            "node_type": "note",
            "visibility": "public",
            "updated_at": "2026-07-21T00:49:50.297882+00:00",
            "score": 0.01639344262295082
        }
    ],
    "total": 1,
    "query": "FastAPI"
}

$ curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8000/api/v1/search?q=FastAPI"
401
```

- [x] **8.5** Full gate:
```bash
cd backend
ruff check .                              # All checks passed!
ruff format --check .                     # 85 files already formatted
mypy app/api app/services app/schemas app/workers   # strict via pyproject; no issues in 31 files
```

- [x] **8.6** Commit:
```
feat(api): GET /api/v1/search — hybrid RRF search endpoint with visibility enforcement
```

---

## Phase 2 exit gate

Run `/kb-verify` and confirm:

```bash
cd backend
pytest tests/ --tb=short              # all green
ruff check .                          # clean
mypy --strict app/services/ app/schemas/  # clean

# Visibility audit — search legs must filter INSIDE CTEs, not after:
grep -n "hybrid_search\|search_service" app/api/v1/search.py
# Must call search_service.hybrid_search (not raw SQL in router)

# Idempotency evidence:
pytest tests/workers/test_embed_node.py::test_embed_node_idempotent -v
pytest tests/workers/test_autolink_node.py::test_autolink_idempotent -v
```

Update `docs/plans/README.md` — Phase 2 Status → `Done`.
