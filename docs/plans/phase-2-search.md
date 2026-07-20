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
- [ ] All tasks checked
- [ ] `pytest -x backend/tests/` green
- [ ] `ruff check backend/` clean
- [ ] `mypy --strict backend/app/services/ backend/app/schemas/` clean
- [ ] `/kb-verify` passes
- [ ] Idempotency test for `embed_node` passes (run twice → no duplicate chunks)
- [ ] `curl` evidence for `GET /api/v1/search?q=...`

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

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.core.db import Base


class NodeChunk(Base):
    __tablename__ = "node_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("node_id", "chunk_index", name="uq_chunk_node_idx"),
    )
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
Revises: 0003
Create Date: 2026-01-01
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_chunks",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("node_id", "chunk_index", name="uq_chunk_node_idx"),
    )
    op.create_index("ix_node_chunks_node_id", "node_chunks", ["node_id"])
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
- Create: `backend/tests/workers/test_celery_app.py`

### Steps

- [ ] **2.1** Write the failing test:

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
    """task_session must be usable as an async context manager."""
    import inspect
    assert inspect.isasyncgenfunction(task_session) or hasattr(task_session, "__aenter__")
```

- [ ] **2.2** Run — expect ImportError:
```bash
cd backend && pytest tests/workers/test_celery_app.py -x 2>&1 | head -10
```

- [ ] **2.3** Implement:

```python
# backend/app/workers/celery_app.py
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

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

- [ ] **2.4** Run tests:
```bash
cd backend && pytest tests/workers/test_celery_app.py -v
# Expected: 3 passed
```

- [ ] **2.5** Commit:
```
feat(workers): Celery app setup with task_session context manager
```

---

## Task 3 — Chunking service

**Files:**
- Create: `backend/app/services/chunking.py`
- Create: `backend/tests/services/test_chunking.py`

### Steps

- [ ] **3.1** Write the failing tests:

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

- [ ] **3.2** Run — expect ImportError:
```bash
cd backend && pytest tests/services/test_chunking.py -x 2>&1 | head -10
```

- [ ] **3.3** Implement:

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

- [ ] **3.4** Run tests:
```bash
cd backend && pytest tests/services/test_chunking.py -v
# Expected: 5 passed
```

- [ ] **3.5** Commit:
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

- [ ] **4.1** Write the failing tests:

```python
# backend/tests/services/test_embedding_service.py
import pytest
from app.services.embedding_service import FakeEmbedder, Embedder, EmbeddingDimension


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

- [ ] **4.2** Implement:

```python
# backend/app/services/embedding_service.py
from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

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
        results = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
            vec = []
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
        self._model = None  # lazy

    def _load(self):
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
    backend = getattr(settings, "embedding_backend", "sentence_transformers")
    if backend == "fake":
        return FakeEmbedder()
    return SentenceTransformersEmbedder(getattr(settings, "embedding_model", "sentence-transformers/all-MiniLM-L12-v2"))
```

- [ ] **4.3** Add `embedding_backend` to Settings:

```python
# backend/app/core/config.py  (add field)
embedding_backend: str = "sentence_transformers"  # fake | sentence_transformers | ollama
embedding_model: str = "sentence-transformers/all-MiniLM-L12-v2"
```

- [ ] **4.4** Add `fake_embedder` to conftest:

```python
# backend/tests/conftest.py (add)
from app.services.embedding_service import FakeEmbedder

@pytest.fixture
def fake_embedder():
    return FakeEmbedder()
```

- [ ] **4.5** Run tests:
```bash
cd backend && pytest tests/services/test_embedding_service.py -v
# Expected: 4 passed
```

- [ ] **4.6** Commit:
```
feat(embedding): Embedder protocol, FakeEmbedder (deterministic), SentenceTransformersEmbedder
```

---

## Task 5 — embed_node Celery task (idempotent)

**Files:**
- Create: `backend/app/workers/tasks/embed_node.py`
- Create: `backend/tests/workers/test_embed_node.py`

### Steps

- [ ] **5.1** Write the failing tests (idempotency test is MANDATORY per kb-celery-jobs):

```python
# backend/tests/workers/test_embed_node.py
import uuid
import pytest
from sqlalchemy import select, func
from app.models.chunk import NodeChunk
from app.services.embedding_service import FakeEmbedder
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_embed_node_creates_chunks(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed1@test.com")
    node = await make_node(owner, body="# Section\n\nThis is content for embedding.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id))
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

    assert count_after_first == count_after_second, "Re-running embed must not create duplicate chunks"


async def test_embed_node_stores_vectors(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed3@test.com")
    node = await make_node(owner, body="Some text to embed.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    chunk = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert chunk.embedding is not None
    assert len(chunk.embedding) == 768
```

- [ ] **5.2** Implement:

```python
# backend/app/workers/tasks/embed_node.py
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.services.chunking import chunk_markdown
from app.services.embedding_service import Embedder, get_embedder
from app.workers.celery_app import celery_app, task_session

if TYPE_CHECKING:
    pass


async def _embed_node_impl(db: AsyncSession, node_id: uuid.UUID, embedder: Embedder) -> None:
    """
    Core logic extracted for unit-testability (no Celery dependency).
    Idempotent: deletes existing chunks for the node before reinserting.
    """
    node = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node_id))
    if node is None or node.deleted_at is not None:
        return

    texts = chunk_markdown(node.body)
    if not texts:
        return

    # Idempotent: replace all chunks
    await db.execute(delete(NodeChunk).where(NodeChunk.node_id == node_id))

    vectors = embedder.embed(texts)

    for idx, (text, vec) in enumerate(zip(texts, vectors)):
        chunk = NodeChunk(
            node_id=node_id,
            chunk_index=idx,
            chunk_text=text,
            embedding=vec,
        )
        db.add(chunk)

    await db.flush()


@celery_app.task(
    bind=True,
    name="kb.embed_node",
    acks_late=True,
    max_retries=3,
    default_retry_delay=30,
)
def embed_node(self, node_id: str) -> None:
    """
    Celery task: chunk and embed a knowledge node.
    Args must be primitives (str, not UUID).
    """
    import asyncio

    nid = uuid.UUID(node_id)
    embedder = get_embedder()

    async def _run():
        async with task_session() as db:
            await _embed_node_impl(db, nid, embedder)

    try:
        asyncio.get_event_loop().run_until_complete(_run())
    except Exception as exc:
        raise self.retry(exc=exc)
```

- [ ] **5.3** Run tests:
```bash
cd backend && pytest tests/workers/test_embed_node.py -v
# Expected: 3 passed (including idempotency test)
```

- [ ] **5.4** Commit:
```
feat(workers): embed_node task — idempotent chunking + vector storage
```

---

## Task 6 — autolink_node Celery task

**Files:**
- Create: `backend/app/workers/tasks/autolink_node.py`
- Create: `backend/tests/workers/test_autolink_node.py`

### Steps

- [ ] **6.1** Write the failing tests:

```python
# backend/tests/workers/test_autolink_node.py
import uuid
import pytest
from sqlalchemy import select, func
from app.models.chunk import NodeChunk
from app.services.embedding_service import FakeEmbedder
from app.workers.tasks.embed_node import _embed_node_impl
from app.workers.tasks.autolink_node import _autolink_node_impl
from app.services import graph_service as gs
from app.services.visibility import Viewer
from app.models.user import Role, Visibility

pytestmark = pytest.mark.asyncio


async def test_autolink_creates_similar_to_edges(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="al1@test.com")
    # FakeEmbedder is deterministic — same text = identical vector = cosine 1.0
    n1 = await make_node(owner, title="Python Tips", body="Python is great for data science.", visibility=Visibility.public)
    n2 = await make_node(owner, title="Python Guide", body="Python is great for data science.", visibility=Visibility.public)
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await _autolink_node_impl(db, n1.id, viewer)

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    edge_labels = [e["label"] for e in hood["edges"]]
    assert "SIMILAR_TO" in edge_labels


async def test_autolink_idempotent(db, make_user, make_node, fake_embedder):
    """Running autolink twice must not create duplicate SIMILAR_TO edges."""
    owner = await make_user(email="al2@test.com")
    n1 = await make_node(owner, title="Topic A", body="Same content here.", visibility=Visibility.public)
    n2 = await make_node(owner, title="Topic B", body="Same content here.", visibility=Visibility.public)
    await db.flush()

    await _embed_node_impl(db, n1.id, fake_embedder)
    await _embed_node_impl(db, n2.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await _autolink_node_impl(db, n1.id, viewer)
    await _autolink_node_impl(db, n1.id, viewer)  # second run

    hood = await gs.get_neighborhood(db, n1.id, viewer, hops=1)
    similar_edges = [e for e in hood["edges"] if e["label"] == "SIMILAR_TO"]
    targets = [e["target"] for e in similar_edges]
    assert len(targets) == len(set(targets)), "Duplicate SIMILAR_TO edges detected"
```

- [ ] **6.2** Implement:

```python
# backend/app/workers/tasks/autolink_node.py
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from celery import shared_task
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.services import graph_service as gs
from app.services.visibility import Viewer, visible_nodes_clause
from app.workers.celery_app import celery_app, task_session

if TYPE_CHECKING:
    pass

_COSINE_THRESHOLD = 0.82
_TOP_K = 5


async def _autolink_node_impl(
    db: AsyncSession,
    node_id: uuid.UUID,
    viewer: Viewer,
) -> None:
    """
    Compute mean-pool vector for node, find top-K similar visible nodes
    (cosine ≥ threshold), and MERGE SIMILAR_TO edges (idempotent via MERGE).
    """
    # Mean-pool: average all chunk embeddings for this node
    result = await db.execute(
        text("""
            SELECT AVG(embedding::float[])::vector AS mean_vec
            FROM node_chunks
            WHERE node_id = :node_id
        """),
        {"node_id": str(node_id)},
    )
    row = result.fetchone()
    if row is None or row[0] is None:
        return

    mean_vec = row[0]

    # Visibility-filtered candidate nodes (owner's own nodes as seed, see ADR-004)
    clause = visible_nodes_clause(viewer)
    candidate_ids_result = await db.scalars(
        select(KnowledgeNode.id).where(clause).where(KnowledgeNode.id != node_id)
    )
    candidate_ids = list(candidate_ids_result)

    if not candidate_ids:
        return

    # Vector similarity search against candidates
    similar_result = await db.execute(
        text("""
            SELECT DISTINCT ON (nc.node_id) nc.node_id,
                   1 - (nc.embedding <=> :query_vec::vector) AS cosine_sim
            FROM node_chunks nc
            WHERE nc.node_id = ANY(:candidate_ids)
              AND nc.embedding IS NOT NULL
            ORDER BY nc.node_id, nc.embedding <=> :query_vec::vector
        """),
        {
            "query_vec": mean_vec,
            "candidate_ids": [str(cid) for cid in candidate_ids],
        },
    )
    similar_rows = similar_result.fetchall()

    # Filter by threshold and take top-K
    similar_rows.sort(key=lambda r: r[1], reverse=True)
    top_k = [r for r in similar_rows if r[1] >= _COSINE_THRESHOLD][:_TOP_K]

    source_node = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == node_id))
    if source_node is None:
        return
    await gs.create_vertex(db, source_node)

    for row in top_k:
        target_id = uuid.UUID(str(row[0]))
        target_node = await db.scalar(select(KnowledgeNode).where(KnowledgeNode.id == target_id))
        if target_node is None:
            continue
        await gs.create_vertex(db, target_node)
        # MERGE is idempotent — calling twice does not create duplicates
        await gs.merge_edge(
            db,
            node_id,
            target_id,
            "SIMILAR_TO",
            props={"created_by": "system:autolink", "cosine": float(row[1])},
        )
        await gs.merge_edge(
            db,
            target_id,
            node_id,
            "SIMILAR_TO",
            props={"created_by": "system:autolink", "cosine": float(row[1])},
        )

    await db.flush()


@celery_app.task(
    bind=True,
    name="kb.autolink_node",
    acks_late=True,
    max_retries=3,
    default_retry_delay=60,
)
def autolink_node(self, node_id: str, user_id: str, role: str, group_ids: list[str]) -> None:
    import asyncio
    from app.models.user import Role
    from app.services.visibility import Viewer

    viewer = Viewer(
        user_id=uuid.UUID(user_id),
        role=Role(role),
        group_ids=frozenset(uuid.UUID(gid) for gid in group_ids),
    )

    async def _run():
        async with task_session() as db:
            await _autolink_node_impl(db, uuid.UUID(node_id), viewer)

    try:
        asyncio.get_event_loop().run_until_complete(_run())
    except Exception as exc:
        raise self.retry(exc=exc)
```

- [ ] **6.3** Run tests:
```bash
cd backend && pytest tests/workers/test_autolink_node.py -v
# Expected: 2 passed
```

- [ ] **6.4** Commit:
```
feat(workers): autolink_node task — mean-pool similarity, MERGE SIMILAR_TO edges, idempotent
```

---

## Task 7 — Hybrid search service

**Files:**
- Create: `backend/app/services/search_service.py`
- Create: `backend/tests/services/test_search_service.py`

### Steps

- [ ] **7.1** Write the failing tests:

```python
# backend/tests/services/test_search_service.py
import pytest
from app.models.user import Visibility, Role
from app.services.visibility import Viewer
from app.services.embedding_service import FakeEmbedder
from app.services import search_service as ss
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_fts_finds_node(db, make_user, make_node):
    owner = await make_user(email="srch_fts@test.com")
    node = await make_node(owner, title="PostgreSQL Tips", body="Full-text search is powerful.", visibility=Visibility.public)
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    results, total = await ss.hybrid_search(db, "PostgreSQL", viewer, fake_embedder=FakeEmbedder())
    ids = [r["id"] for r in results]
    assert str(node.id) in ids


async def test_vector_finds_similar(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="srch_vec@test.com")
    node = await make_node(owner, title="Vector Node", body="embeddings and similarity search", visibility=Visibility.public)
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    results, total = await ss.hybrid_search(db, "embeddings similarity", viewer, fake_embedder=fake_embedder)
    ids = [r["id"] for r in results]
    assert str(node.id) in ids


async def test_private_node_excluded_from_search(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="srch_priv@test.com")
    other = await make_user(email="srch_priv2@test.com")
    node = await make_node(owner, title="Secret", body="private secret content", visibility=Visibility.private)
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    results, _ = await ss.hybrid_search(db, "secret private", viewer, fake_embedder=fake_embedder)
    ids = [r["id"] for r in results]
    assert str(node.id) not in ids, "Private node must not appear in another user's search"
```

- [ ] **7.2** Implement `search_service.py` with exact RRF fusion from kb-pgvector-search skill:

```python
# backend/app/services/search_service.py
"""
Hybrid full-text + vector search with RRF fusion.

CRITICAL invariants (ADR-004 / kb-visibility-filter):
- Visibility filter applied INSIDE each CTE leg, before LIMIT
- Never post-filter after merge — that would allow LIMIT to cut visible results
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.embedding_service import Embedder
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
    fake_embedder: Embedder | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Hybrid RRF search: FTS leg + vector leg fused with Reciprocal Rank Fusion.
    Returns (results, total_count).

    score = Σ 1 / (k + rank_i)   where k=60
    """
    from app.services.embedding_service import get_embedder

    embedder = fake_embedder or get_embedder()
    query_vec = embedder.embed([query])[0]

    # Build visibility predicate as a subquery node_id list
    # Build visibility predicate as a subquery node_id list
    from sqlalchemy import select
    from app.models.knowledge import KnowledgeNode

    clause = visible_nodes_clause(viewer)
    visible_ids_result = await db.scalars(
        select(KnowledgeNode.id).where(clause)
    )
    visible_ids = [str(i) for i in visible_ids_result]

    if not visible_ids:
        return [], 0

    # Set ef_search for this session (HNSW quality vs speed)
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {_EF_SEARCH}"))

    vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"

    sql = text("""
        WITH visible AS (
            SELECT id FROM knowledge_nodes
            WHERE id = ANY(:visible_ids::uuid[])
        ),
        fts_ranked AS (
            SELECT kn.id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(kn.body_tsv, query) DESC) AS rank
            FROM knowledge_nodes kn,
                 to_tsquery('english', :tsquery) AS query
            WHERE kn.id IN (SELECT id FROM visible)
              AND kn.body_tsv @@ query
            LIMIT 100
        ),
        vec_ranked AS (
            SELECT DISTINCT ON (nc.node_id) nc.node_id AS id,
                   ROW_NUMBER() OVER (ORDER BY nc.embedding <=> :query_vec::vector) AS rank
            FROM node_chunks nc
            WHERE nc.node_id IN (SELECT id FROM visible)
              AND nc.embedding IS NOT NULL
            ORDER BY nc.node_id, nc.embedding <=> :query_vec::vector
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
        ORDER BY rrf.score DESC
        LIMIT :limit OFFSET :offset
    """)

    count_sql = text("""
        WITH visible AS (SELECT id FROM knowledge_nodes WHERE id = ANY(:visible_ids::uuid[])),
        fts_ids AS (
            SELECT kn.id FROM knowledge_nodes kn, to_tsquery('english', :tsquery) AS q
            WHERE kn.id IN (SELECT id FROM visible) AND kn.body_tsv @@ q
        ),
        vec_ids AS (
            SELECT DISTINCT nc.node_id AS id FROM node_chunks nc
            WHERE nc.node_id IN (SELECT id FROM visible) AND nc.embedding IS NOT NULL
        )
        SELECT COUNT(DISTINCT id) FROM (SELECT id FROM fts_ids UNION SELECT id FROM vec_ids) sub
    """)

    # Convert query to tsquery (simple: replace spaces with & for AND logic)
    tsquery = " & ".join(query.split())

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

- [ ] **7.3** Run tests:
```bash
cd backend && pytest tests/services/test_search_service.py -v
# Expected: 3 passed
```

- [ ] **7.4** Commit:
```
feat(search): hybrid RRF search (FTS + pgvector) with visibility inside each leg
```

---

## Task 8 — Search API endpoint

**Files:**
- Create: `backend/app/api/v1/search.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_search_api.py`

### Steps

- [ ] **8.1** Write failing tests:

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

- [ ] **8.2** Create the router:

```python
# backend/app/api/v1/search.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_viewer
from app.services import search_service as ss

router = APIRouter(prefix="/search", tags=["search"])


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


@router.get("", response_model=SearchOut)
async def search(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    viewer=Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
):
    results, total = await ss.hybrid_search(db, q, viewer, limit=limit, offset=offset)
    return SearchOut(items=results, total=total, query=q)
```

- [ ] **8.3** Register in `main.py`:

```python
from app.api.v1 import search as search_router
app.include_router(search_router.router, prefix="/api/v1")
```

- [ ] **8.4** Run all tests + curl evidence:
```bash
cd backend && pytest tests/ -v --tb=short
# Expected: all pass

curl -s "http://localhost:8000/api/v1/search?q=FastAPI" \
  -H "Authorization: Bearer $TOKEN" | jq '{total: .total, first: .items[0].title}'
```

- [ ] **8.5** Full gate:
```bash
cd backend
ruff check .
mypy --strict app/services/ app/schemas/
```

- [ ] **8.6** Commit:
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
