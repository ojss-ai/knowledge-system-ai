# Phase 7 — RAG, Admin & Hardening

**Goal:** Add a `/ask` RAG endpoint (retrieval-augmented generation with on-prem LLM), auto-tag suggestion, admin dashboards (API + frontend), audit logging, JWT refresh token revocation, rate limiting, and a load test gate.

**Architecture refs:** ADR-010 (on-prem LLM, feature flag LLM_ALLOW_EXTERNAL=false), ADR-004 (visibility — RAG must respect viewer), ADR-008 (JWT revocation list)

**Required skills (read before any task):**
- `kb-conventions`
- `kb-tdd-workflow`
- `kb-visibility-filter` — RAG retrieval must go through visibility.py
- `kb-api-conventions`
- `kb-celery-jobs` (auto-tag is async)

**Exit criteria:**
- [ ] All tasks checked
- [ ] `pytest -x backend/tests/` green
- [ ] `ruff check backend/` clean
- [ ] `mypy --strict backend/app/services/ backend/app/schemas/` clean
- [ ] `/kb-verify` passes full gate
- [ ] Load test: `locust` or `k6` at 50 RPS for 60s with p95 latency < 500ms
- [ ] LLM_ALLOW_EXTERNAL=false graceful degradation tested

---

## Task 1 — LLM service adapter

**Files:**
- Create: `backend/app/services/llm_service.py`
- Create: `backend/tests/services/test_llm_service.py`

### Steps

- [x] **1.1** Write the failing tests: *([plan-fix]: dropped unused `import pytest` — ruff F401)*

```python
# backend/tests/services/test_llm_service.py
from app.services.llm_service import FakeLLM, LLMAdapter, get_llm


def test_fake_llm_returns_string():
    llm = FakeLLM()
    result = llm.complete("Summarise: hello world")
    assert isinstance(result, str)
    assert len(result) > 0


def test_fake_llm_is_adapter():
    llm = FakeLLM()
    assert isinstance(llm, LLMAdapter)


def test_get_llm_returns_fake_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ALLOW_EXTERNAL", "false")
    monkeypatch.setenv("LLM_BACKEND", "fake")
    llm = get_llm()
    assert isinstance(llm, FakeLLM)


def test_llm_complete_accepts_system_prompt():
    llm = FakeLLM()
    result = llm.complete("What is Python?", system="You are a helpful assistant.")
    assert isinstance(result, str)
```

- [x] **1.2** Implement: *([plan-fix]: `requests` is not a backend dependency — OllamaLLM uses `httpx` (moved to runtime deps); OpenAIAdapter fully typed and `openai.*` mypy override added so `mypy --strict app/services/` passes)*

```python
# backend/app/services/llm_service.py
"""
LLM service adapter — on-prem first (ADR-010).

Feature flag:
  LLM_ALLOW_EXTERNAL=false (default) → only local backends (Ollama, fake)
  LLM_ALLOW_EXTERNAL=true            → external APIs allowed (OpenAI)

Backends:
  LLM_BACKEND=fake            → FakeLLM (tests + graceful degradation)
  LLM_BACKEND=ollama          → Ollama local server
  LLM_BACKEND=openai          → OpenAI API (requires LLM_ALLOW_EXTERNAL=true)
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMAdapter(Protocol):
    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        """Send a prompt and return the text completion."""
        ...


class FakeLLM:
    """
    Deterministic fake for tests and graceful degradation fallback.
    Returns a templated string so tests can assert on structure.
    """

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        return f"[FakeLLM response to: {prompt[:80]}]"


class OllamaLLM:
    """
    Adapter for a local Ollama server (HTTP API via httpx).
    On any failure the adapter degrades gracefully to a stub response.
    """

    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
    ) -> None:
        self._model = model
        self._base_url = base_url

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        try:
            import httpx

            payload = {
                "model": self._model,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {"num_predict": max_tokens},
            }
            r = httpx.post(f"{self._base_url}/api/generate", json=payload, timeout=60)
            r.raise_for_status()
            response = r.json().get("response", "")
            return response if isinstance(response, str) else ""
        except Exception as exc:
            # Graceful degradation: fall back to stub
            return f"[LLM unavailable: {exc}]"


def get_llm() -> LLMAdapter:
    """
    Factory respecting the LLM_ALLOW_EXTERNAL feature flag.
    If LLM_ALLOW_EXTERNAL=false, external backends are silently downgraded to FakeLLM.
    """
    allow_external = os.environ.get("LLM_ALLOW_EXTERNAL", "false").lower() == "true"
    backend = os.environ.get("LLM_BACKEND", "ollama")

    if backend == "fake":
        return FakeLLM()

    if backend == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "llama3")
        url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaLLM(model=model, base_url=url)

    if backend == "openai":
        if not allow_external:
            # ADR-010: external APIs blocked by default
            return FakeLLM()
        # openai backend (optional dep)
        try:
            from openai import OpenAI

            class OpenAIAdapter:
                def __init__(self) -> None:
                    self._client = OpenAI()
                    self._model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

                def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
                    msgs: list[dict[str, str]] = []
                    if system:
                        msgs.append({"role": "system", "content": system})
                    msgs.append({"role": "user", "content": prompt})
                    r: Any = self._client.chat.completions.create(
                        model=self._model, messages=msgs, max_tokens=max_tokens
                    )
                    content = r.choices[0].message.content
                    return content if isinstance(content, str) else ""

            return OpenAIAdapter()
        except ImportError:
            return FakeLLM()

    return FakeLLM()
```

- [x] **1.3** Add to `config.py` (also in pyproject: `httpx>=0.27` moved dev → runtime deps; `[[tool.mypy.overrides]] module = "openai.*"` added):
```python
llm_backend: str = "ollama"  # fake | ollama | openai
llm_allow_external: bool = False
ollama_model: str = "llama3"
ollama_base_url: str = "http://localhost:11434"
```

- [x] **1.4** Run tests:
```bash
cd backend && pytest tests/services/test_llm_service.py -v
# Expected: 4 passed
```

- [x] **1.5** Commit:
```
feat(llm): LLMAdapter protocol, FakeLLM, OllamaLLM, get_llm() with LLM_ALLOW_EXTERNAL gate
```

---

## Task 2 — RAG service + /ask endpoint

**Files:**
- Create: `backend/app/services/rag_service.py`
- Create: `backend/app/api/v1/ask.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/services/test_rag_service.py`
- Create: `backend/tests/api/test_ask_api.py`

### Steps

- [x] **2.1** Write failing tests for RAG service ([plan-fix]: dropped unused `FakeEmbedder` import (ruff F401); added answer-content leak assertions — kb-visibility-filter's mandatory test covers content AND existence):

```python
# backend/tests/services/test_rag_service.py
import pytest
from app.models.user import Role, Visibility
from app.services import rag_service as rag
from app.services.llm_service import FakeLLM
from app.services.visibility import Viewer
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_rag_returns_answer(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="rag1@test.com")
    node = await make_node(owner, title="Python Guide",
                          body="Python is great for data science and ML pipelines.",
                          visibility=Visibility.public)
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    result = await rag.ask(
        db, "What is Python good for?", viewer,
        embedder=fake_embedder, llm=FakeLLM(),
    )
    assert result.answer is not None
    assert len(result.answer) > 0
    assert isinstance(result.sources, list)


async def test_rag_respects_visibility(db, make_user, make_node, fake_embedder):
    """RAG must not use private nodes in context for other users."""
    owner = await make_user(email="rag_v1@test.com")
    other = await make_user(email="rag_v2@test.com")
    node = await make_node(owner, title="Secret",
                          body="Top secret classified information.",
                          visibility=Visibility.private)
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    result = await rag.ask(
        db, "secret classified", viewer,
        embedder=fake_embedder, llm=FakeLLM(),
    )
    source_ids = [s["id"] for s in result.sources]
    assert str(node.id) not in source_ids, "Private node must not appear in RAG context"
    # [plan-fix] content leak check: FakeLLM echoes its prompt, so a leaked
    # context would surface the private body/title in the answer.
    assert "Top secret" not in result.answer
    assert "Secret" not in result.answer
```

- [x] **2.2** Implement RAG service ([plan-fix]: hoisted the loop-body imports to module top (ruff I001/style); extracted `_NO_CONTEXT_ANSWER` constant; system prompt reformatted for the 100-col limit — logic unchanged):

```python
# backend/app/services/rag_service.py
"""
Retrieval-Augmented Generation service.

CRITICAL: Visibility filter applied to retrieval — never expose private nodes
in context, even if the LLM would not directly reveal them (ADR-004).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode
from app.services import search_service as ss
from app.services.embedding_service import Embedder
from app.services.llm_service import LLMAdapter
from app.services.visibility import Viewer, visible_nodes_clause

_CONTEXT_LIMIT = 5
_CONTEXT_MAX_CHARS = 8000

_SYSTEM_PROMPT = (
    "You are a helpful assistant answering questions from a personal knowledge base.\n"
    "Use ONLY the provided context to answer. If the answer is not in the context, say "
    '"I don\'t have enough information in the knowledge base to answer that."\n'
    "Be concise and cite the source titles when relevant."
)

_NO_CONTEXT_ANSWER = "I don't have enough information in the knowledge base to answer that."


@dataclass
class RAGResult:
    answer: str
    sources: list[dict[str, Any]]
    query: str


async def ask(
    db: AsyncSession,
    query: str,
    viewer: Viewer,
    *,
    embedder: Embedder,
    llm: LLMAdapter,
    limit: int = _CONTEXT_LIMIT,
) -> RAGResult:
    """
    Retrieve relevant context for `query` (visibility-filtered),
    then call LLM with context to generate an answer.
    """
    # Step 1: hybrid search with visibility filter (same as /search endpoint)
    results, _ = await ss.hybrid_search(db, query, viewer, limit=limit, fake_embedder=embedder)

    if not results:
        return RAGResult(answer=_NO_CONTEXT_ANSWER, sources=[], query=query)

    # Step 2: build context string
    context_parts: list[str] = []
    total_chars = 0
    for result in results:
        # Fetch body for context — re-checked through the visibility choke point
        node = await db.scalar(
            select(KnowledgeNode).where(
                KnowledgeNode.id == uuid.UUID(result["id"]),
                visible_nodes_clause(viewer),
            )
        )
        if node is None:
            continue

        chunk = f"### {node.title}\n{node.body}"
        if total_chars + len(chunk) > _CONTEXT_MAX_CHARS:
            break
        context_parts.append(chunk)
        total_chars += len(chunk)

    if not context_parts:
        return RAGResult(answer=_NO_CONTEXT_ANSWER, sources=[], query=query)

    context = "\n\n---\n\n".join(context_parts)
    prompt = f"Context:\n\n{context}\n\n---\n\nQuestion: {query}"

    # Step 3: LLM completion
    answer = llm.complete(prompt, system=_SYSTEM_PROMPT)

    return RAGResult(
        answer=answer,
        sources=results[:limit],
        query=query,
    )
```

- [x] **2.3** Write failing API test:

```python
# backend/tests/api/test_ask_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_ask_returns_answer(client: AsyncClient, auth_headers):
    # Create a node with content
    await client.post("/api/v1/nodes", json={
        "title": "FastAPI",
        "body": "FastAPI is a modern Python web framework.",
        "visibility": "public",
    }, headers=auth_headers)

    r = await client.post(
        "/api/v1/ask",
        json={"query": "What is FastAPI?"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data
    assert "sources" in data
    assert isinstance(data["sources"], list)


async def test_ask_requires_auth(client: AsyncClient):
    r = await client.post("/api/v1/ask", json={"query": "test"})
    assert r.status_code == 401
```

- [x] **2.4** Create the router ([plan-fix]: `get_current_viewer` → `get_scoped_viewer` — /ask is not an admin-console route, so the admin visibility bypass must be scoped down (kb-visibility-filter rule 5, matches every other non-admin router); added `summary`/`operation_id` per kb-api-conventions; bounded `query`/`limit` fields for 422 validation; typed signature):

```python
# backend/app/api/v1/ask.py
"""RAG /ask router — thin translation layer over rag_service (ADR-005).

Retrieval is visibility-filtered inside rag_service (kb-visibility-filter
rule 4): citations can only reference nodes the caller can read.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer
from app.services import rag_service as rag
from app.services.embedding_service import get_embedder
from app.services.llm_service import get_llm

router = APIRouter(prefix="/ask", tags=["rag"])


class AskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(5, ge=1, le=20)


class AskResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]]
    query: str


@router.post(
    "",
    response_model=AskResponse,
    summary="Ask the knowledge base",
    operation_id="askKnowledgeBase",
)
async def ask(
    payload: AskRequest,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> AskResponse:
    result = await rag.ask(
        db,
        payload.query,
        viewer,
        embedder=get_embedder(),
        llm=get_llm(),
        limit=payload.limit,
    )
    return AskResponse(answer=result.answer, sources=result.sources, query=result.query)
```

- [x] **2.5** Register in `main.py` ([plan-fix]: import style matches the existing `from app.api.v1.<mod> import router as <mod>_router` pattern):
```python
from app.api.v1.ask import router as ask_router
app.include_router(ask_router, prefix="/api/v1")
```

- [x] **2.6** Run tests:
```bash
cd backend && pytest tests/services/test_rag_service.py tests/api/test_ask_api.py -v
# Expected: 4 passed
# Actual: 4 passed in 1.17s (full suite: 213 passed, 13 Neo4j-skips)
```

- [x] **2.7** Commit:
```
feat(rag): rag_service with visibility-filtered retrieval + POST /api/v1/ask
```

---

## Task 3 — Audit log model + service

**Files:**
- Create: `backend/app/models/audit.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/0006_audit_log.py`
- Create: `backend/app/services/audit_service.py`
- Create: `backend/tests/services/test_audit_service.py`

### Steps

- [ ] **3.1** Write failing test:

```python
# backend/tests/services/test_audit_service.py
import pytest
from app.services import audit_service as audit
from app.models.user import Role
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_log_action(db, make_user):
    owner = await make_user(email="audit1@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await audit.log(db, viewer=viewer, action="node.create", resource_type="node",
                    resource_id=str(owner.id), meta={"title": "Test"})
    await db.flush()
    from sqlalchemy import select
    from app.models.audit import AuditLog
    rows = await db.scalars(select(AuditLog).where(AuditLog.actor_id == owner.id))
    entries = list(rows)
    assert len(entries) == 1
    assert entries[0].action == "node.create"
```

- [ ] **3.2** Create model:

```python
# backend/app/models/audit.py
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(128), nullable=False)       # "node.create", "node.delete" etc.
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(256))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_audit_actor", "actor_id"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_created_at", "created_at"),
    )
```

- [ ] **3.3** Create audit service:

```python
# backend/app/services/audit_service.py
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.services.visibility import Viewer


async def log(
    db: AsyncSession,
    *,
    viewer: Viewer | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=viewer.user_id if viewer else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        meta=meta or {},
    )
    db.add(entry)
    # Non-blocking: do not flush here — caller's transaction commits it
```

- [ ] **3.4** Migration `0006_audit_log.py`:

```python
# backend/alembic/versions/0006_audit_log.py
"""audit_logs table

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(256)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_actor", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_action", "audit_logs", ["action"])
    op.create_index("ix_audit_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
```

- [ ] **3.5** Apply and run tests:
```bash
cd backend && alembic upgrade head
pytest tests/services/test_audit_service.py -v
# Expected: 1 passed
```

- [ ] **3.6** Commit:
```
feat(audit): AuditLog model + migration 0006 + audit_service.log()
```

---

## Task 4 — JWT refresh revocation

**Files:**
- Modify: `backend/app/core/security.py`
- Modify: `backend/app/api/v1/auth.py`
- Create: `backend/tests/api/test_token_revocation.py`

### Steps

- [ ] **4.1** Write failing test:

```python
# backend/tests/api/test_token_revocation.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_refresh_token_used_twice_rejected(client: AsyncClient):
    """Using a refresh token a second time must return 401 (rotation + revocation)."""
    await client.post("/api/v1/auth/register", json={
        "email": "revoke@test.com", "password": "pass1234", "display_name": "R"
    })
    r = await client.post("/api/v1/auth/login",
        data={"username": "revoke@test.com", "password": "pass1234"})
    refresh_token = r.json()["refresh_token"]

    # First refresh — should succeed
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r1.status_code == 200

    # Second use of same refresh token — must be rejected
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r2.status_code == 401
```

- [ ] **4.2** Implement revocation list in Redis:

```python
# backend/app/core/security.py  (add to existing file)
import redis.asyncio as aioredis
from app.core.config import settings

_redis: aioredis.Redis | None = None

async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def revoke_jti(jti: str, ttl_seconds: int) -> None:
    """Add a JTI to the revocation set in Redis."""
    r = await _get_redis()
    await r.setex(f"revoked_jti:{jti}", ttl_seconds, "1")


async def is_jti_revoked(jti: str) -> bool:
    r = await _get_redis()
    return await r.exists(f"revoked_jti:{jti}") == 1
```

- [ ] **4.3** Update refresh endpoint in `auth.py` to revoke old JTI on use:

```python
# backend/app/api/v1/auth.py — refresh endpoint addition
# In the refresh route, after decoding the old refresh token:
#   1. Check if jti is revoked → 401 if so
#   2. Revoke old jti (with remaining TTL)
#   3. Issue new access + refresh tokens

@router.post("/refresh", response_model=TokensOut)
async def refresh_tokens(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    from app.core.security import decode_token, revoke_jti, is_jti_revoked, create_access_token, create_refresh_token
    try:
        claims = decode_token(payload.refresh_token, expected_kind="refresh")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    jti = claims.get("jti")
    if jti and await is_jti_revoked(jti):
        raise HTTPException(status_code=401, detail="Refresh token already used")

    if jti:
        import time
        remaining = int(claims["exp"] - time.time())
        if remaining > 0:
            await revoke_jti(jti, remaining)

    user = await db.scalar(select(User).where(User.id == uuid.UUID(claims["sub"])))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    return TokensOut(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
    )
```

- [ ] **4.4** Run tests:
```bash
cd backend && pytest tests/api/test_token_revocation.py -v
# Expected: 1 passed
```

- [ ] **4.5** Commit:
```
feat(auth): JWT refresh token revocation via Redis JTI blocklist
```

---

## Task 5 — Rate limiting middleware

**Files:**
- Create: `backend/app/core/rate_limit.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_rate_limit.py`

### Steps

- [ ] **5.1** Write failing test:

```python
# backend/tests/api/test_rate_limit.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_rate_limit_on_ask_endpoint(client: AsyncClient, auth_headers):
    """The /ask endpoint should rate-limit after N requests per window."""
    # Send many requests rapidly
    responses = []
    for _ in range(30):
        r = await client.post("/api/v1/ask", json={"query": "test"}, headers=auth_headers)
        responses.append(r.status_code)

    # At least one should be 429
    assert 429 in responses, "Rate limiting must return 429 after burst"
```

- [ ] **5.2** Implement sliding-window rate limiter using Redis:

```python
# backend/app/core/rate_limit.py
"""
Redis sliding-window rate limiter.
Applied per user_id to expensive endpoints (/ask, /search).
"""
from __future__ import annotations

import time

from fastapi import HTTPException, Request, status

_LIMITS: dict[str, tuple[int, int]] = {
    "/api/v1/ask": (20, 60),      # 20 requests per 60 seconds
    "/api/v1/search": (60, 60),   # 60 requests per 60 seconds
}


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    limit_config = None
    for pattern, config in _LIMITS.items():
        if path.startswith(pattern):
            limit_config = config
            break

    if limit_config is None:
        return await call_next(request)

    max_requests, window_seconds = limit_config

    # Get user identity from JWT (if present)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await call_next(request)

    import hashlib
    user_key = hashlib.sha256(auth.encode()).hexdigest()[:16]
    redis_key = f"rate:{path}:{user_key}"

    try:
        from app.core.security import _get_redis
        r = await _get_redis()
        now = time.time()
        window_start = now - window_seconds

        # Sliding window using sorted set
        pipe = r.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zadd(redis_key, {str(now): now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        results = await pipe.execute()
        count = results[2]

        if count > max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {max_requests} requests per {window_seconds}s",
                headers={"Retry-After": str(window_seconds)},
            )
    except HTTPException:
        raise
    except Exception:
        # Redis unavailable — fail open (don't block legitimate requests)
        pass

    return await call_next(request)
```

- [ ] **5.3** Register middleware in `main.py`:

```python
# backend/app/main.py (add in create_app)
from app.core.rate_limit import rate_limit_middleware
from starlette.middleware.base import BaseHTTPMiddleware
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
```

- [ ] **5.4** Run tests:
```bash
cd backend && pytest tests/api/test_rate_limit.py -v
# Expected: 1 passed
```

- [ ] **5.5** Commit:
```
feat(api): Redis sliding-window rate limiting on /ask and /search (20/60s, 60/60s)
```

---

## Task 6 — Admin API + dashboards

**Files:**
- Create: `backend/app/api/v1/admin.py`
- Modify: `backend/app/main.py`
- Create: `frontend/src/app/admin/page.tsx`
- Create: `backend/tests/api/test_admin_api.py`

### Steps

- [ ] **6.1** Write failing tests:

```python
# backend/tests/api/test_admin_api.py
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_admin_stats_requires_admin(client: AsyncClient, auth_headers):
    r = await client.get("/api/v1/admin/stats", headers=auth_headers)
    assert r.status_code == 403


async def test_admin_stats_for_admin(client: AsyncClient):
    # Login as seeded admin
    r = await client.post("/api/v1/auth/login",
        data={"username": "admin@kb.local", "password": "admin1234"})
    token = r.json()["access_token"]
    r2 = await client.get("/api/v1/admin/stats",
        headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    data = r2.json()
    assert "total_users" in data
    assert "total_nodes" in data
    assert "total_chunks" in data


async def test_admin_audit_log(client: AsyncClient):
    r = await client.post("/api/v1/auth/login",
        data={"username": "admin@kb.local", "password": "admin1234"})
    token = r.json()["access_token"]
    r2 = await client.get("/api/v1/admin/audit-logs",
        headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert "items" in r2.json()
```

- [ ] **6.2** Create admin router:

```python
# backend/app/api/v1/admin.py
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_viewer, require_admin
from app.models.audit import AuditLog
from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class StatsOut(BaseModel):
    total_users: int
    active_users: int
    total_nodes: int
    total_chunks: int
    total_audit_events: int


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    created_at: datetime
    meta: dict

    model_config = {"from_attributes": True}


class AuditLogsListOut(BaseModel):
    items: list[AuditLogOut]
    total: int


@router.get("/stats", response_model=StatsOut)
async def get_stats(db: AsyncSession = Depends(get_db)):
    total_users = await db.scalar(select(func.count()).select_from(User)) or 0
    active_users = await db.scalar(select(func.count()).select_from(User).where(User.is_active == True)) or 0
    total_nodes = await db.scalar(
        select(func.count()).select_from(KnowledgeNode).where(KnowledgeNode.deleted_at.is_(None))
    ) or 0
    total_chunks = await db.scalar(select(func.count()).select_from(NodeChunk)) or 0
    total_audit = await db.scalar(select(func.count()).select_from(AuditLog)) or 0
    return StatsOut(
        total_users=total_users,
        active_users=active_users,
        total_nodes=total_nodes,
        total_chunks=total_chunks,
        total_audit_events=total_audit,
    )


@router.get("/audit-logs", response_model=AuditLogsListOut)
async def get_audit_logs(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    action: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        q = q.where(AuditLog.action == action)
    total = await db.scalar(select(func.count()).select_from(AuditLog))
    rows = await db.scalars(q.offset(offset).limit(limit))
    return AuditLogsListOut(items=list(rows), total=total or 0)


@router.get("/users", response_model=list[dict])
async def list_all_users(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(select(User).order_by(User.created_at.desc()).offset(offset).limit(limit))
    return [
        {"id": str(u.id), "email": u.email, "role": u.role.value,
         "is_active": u.is_active, "created_at": u.created_at.isoformat()}
        for u in rows
    ]
```

- [ ] **6.3** Add `require_admin` dep if not already in `deps.py`:

```python
# backend/app/core/deps.py (add)
from fastapi import HTTPException
from app.models.user import Role

async def require_admin(viewer=Depends(get_current_viewer)):
    if viewer.role != Role.admin:
        raise HTTPException(status_code=403, detail="Admin required")
    return viewer
```

- [ ] **6.4** Register in `main.py`:
```python
from app.api.v1 import admin as admin_router
app.include_router(admin_router.router, prefix="/api/v1")
```

- [ ] **6.5** Create admin frontend page:

```typescript
// frontend/src/app/admin/page.tsx
"use client"
import { useQuery } from "@tanstack/react-query"
import Sidebar from "@/components/Sidebar"

async function fetchStats() {
  const r = await fetch("/api/v1/admin/stats", { credentials: "include" })
  if (!r.ok) throw new Error("Not authorized")
  return r.json()
}

export default function AdminPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ["admin-stats"], queryFn: fetchStats })

  return (
    <div className="flex h-screen">
      <Sidebar />
      <main className="flex-1 p-6 overflow-auto">
        <h1 className="text-xl font-bold mb-6">Admin Dashboard</h1>
        {isLoading && <p className="text-gray-400">Loading…</p>}
        {error && <p className="text-red-400">Access denied or error loading stats</p>}
        {data && (
          <div className="grid grid-cols-3 gap-4">
            {[
              { label: "Total Users", value: data.total_users },
              { label: "Active Users", value: data.active_users },
              { label: "Total Nodes", value: data.total_nodes },
              { label: "Total Chunks", value: data.total_chunks },
              { label: "Audit Events", value: data.total_audit_events },
            ].map((stat) => (
              <div key={stat.label} className="bg-gray-900 rounded-xl p-5">
                <p className="text-xs text-gray-500 uppercase tracking-wider">{stat.label}</p>
                <p className="text-3xl font-bold mt-1">{stat.value.toLocaleString()}</p>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
```

- [ ] **6.6** Run tests:
```bash
cd backend && pytest tests/api/test_admin_api.py -v
# Expected: 3 passed
```

- [ ] **6.7** Commit:
```
feat(admin): GET /api/v1/admin/stats, /audit-logs, /users + admin dashboard page
```

---

## Task 7 — Load test

**Files:**
- Create: `backend/tests/load/locustfile.py`

### Steps

- [ ] **7.1** Create Locust load test:

```python
# backend/tests/load/locustfile.py
"""
Load test for the Knowledge Base API.
Run: locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s --host http://localhost:8000

Target: p95 latency < 500ms at 50 concurrent users.
"""
import json
import random
from locust import HttpUser, between, task


SAMPLE_QUERIES = [
    "Python", "FastAPI", "graph database", "knowledge base",
    "embeddings", "vector search", "Confluence", "daily log",
]


class KBUser(HttpUser):
    wait_time = between(0.1, 1.0)
    _token: str = ""

    def on_start(self):
        """Log in and store access token."""
        r = self.client.post(
            "/api/v1/auth/login",
            data={"username": "admin@kb.local", "password": "admin1234"},
        )
        self._token = r.json().get("access_token", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    @task(5)
    def search(self):
        q = random.choice(SAMPLE_QUERIES)
        self.client.get(f"/api/v1/search?q={q}", headers=self._headers(), name="/api/v1/search")

    @task(3)
    def list_nodes(self):
        self.client.get("/api/v1/nodes?limit=20", headers=self._headers(), name="/api/v1/nodes")

    @task(1)
    def graph_overview(self):
        self.client.get("/api/v1/graph/overview?limit=50", headers=self._headers(), name="/api/v1/graph/overview")

    @task(1)
    def ask(self):
        q = random.choice(SAMPLE_QUERIES)
        self.client.post(
            "/api/v1/ask",
            json={"query": q},
            headers=self._headers(),
            name="/api/v1/ask",
        )
```

- [ ] **7.2** Run load test (requires running stack + locust installed):
```bash
pip install locust --break-system-packages
cd backend
locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s \
  --host http://localhost:8000 --html /tmp/load_report.html
# Check: p95 < 500ms, 0% error rate on non-rate-limited endpoints
```

- [ ] **7.3** Commit:
```
test(load): Locust load test at 50 RPS targeting p95 < 500ms
```

---

## Task 8 — Final /kb-verify gate

### Steps

- [ ] **8.1** Run full backend test suite:
```bash
cd backend
pytest tests/ -v --tb=short --cov=app --cov-report=term-missing
# Expected: all pass, coverage > 80% on app/services/
```

- [ ] **8.2** Run lint + type check:
```bash
cd backend
ruff check .
mypy --strict app/services/ app/schemas/
```

- [ ] **8.3** Run visibility audit:
```bash
# Must return 0 raw queries on knowledge_nodes outside authorised files
grep -rn "SELECT.*knowledge_nodes\|from knowledge_nodes" \
  backend/app/api/ backend/app/services/ \
  | grep -v "visibility.py\|node_service.py\|graph_service.py\|search_service.py\|rag_service.py\|daily_logs.py\|admin.py"
# Expected: 0 lines
```

- [ ] **8.4** Run alembic upgrade (clean from scratch):
```bash
cd backend
alembic downgrade base
alembic upgrade head
# Expected: no errors
```

- [ ] **8.5** Frontend build:
```bash
cd frontend && npm run build && npm run lint && npx vitest run
```

- [ ] **8.6** CLI tools:
```bash
cd tools/kb-confluence-sync && python -m pytest tests/ -v
cd tools/kb-codebase-scan && python -m pytest tests/ -v
```

- [ ] **8.7** Generate OpenAPI spec:
```bash
cd backend && python app/scripts/export_openapi.py
# Verify spec is valid JSON with all expected paths
```

- [ ] **8.8** Commit:
```
chore: phase 7 complete — full verify gate passed
```

---

## Phase 7 exit gate

```bash
# Full verification
cd backend
pytest tests/ --tb=short -q                         # all green
ruff check .                                          # clean
mypy --strict app/services/ app/schemas/             # clean

# LLM_ALLOW_EXTERNAL=false graceful degradation:
LLM_BACKEND=fake LLM_ALLOW_EXTERNAL=false \
  python -c "from app.services.llm_service import get_llm, FakeLLM; assert isinstance(get_llm(), FakeLLM)"
echo "Graceful degradation: OK"

# Load test (must pass before phase is Done):
locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s \
  --host http://localhost:8000
# p95 < 500ms

# Revocation test:
pytest tests/api/test_token_revocation.py -v

# Rate limit test:
pytest tests/api/test_rate_limit.py -v
```

Update `docs/plans/README.md` — Phase 7 Status → `Done`.

**🎉 All 8 phases complete. Run `/kb-status` for final summary.**
