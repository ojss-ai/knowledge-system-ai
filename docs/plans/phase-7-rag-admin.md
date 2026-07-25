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

- [x] **1.1** Write the failing tests: *([plan-fix]: dropped unused `import pytest` — ruff F401; [1.R]: async `complete`, settings-driven factory)*

```python
# backend/tests/services/test_llm_service.py
import inspect

from app.core.config import settings
from app.services.llm_service import FakeLLM, LLMAdapter, OllamaLLM, get_llm


async def test_fake_llm_returns_string():
    llm = FakeLLM()
    result = await llm.complete("Summarise: hello world")
    assert isinstance(result, str)
    assert len(result) > 0


def test_fake_llm_is_adapter():
    llm = FakeLLM()
    assert isinstance(llm, LLMAdapter)


def test_adapter_complete_is_async():
    # 1.R.1: a sync complete() (blocking httpx.post) inside the async /ask path
    # stalls the event loop for up to 60s — every adapter must be awaitable.
    assert inspect.iscoroutinefunction(FakeLLM.complete)
    assert inspect.iscoroutinefunction(OllamaLLM.complete)


def test_get_llm_returns_fake_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "llm_allow_external", False)
    monkeypatch.setattr(settings, "llm_backend", "fake")
    llm = get_llm()
    assert isinstance(llm, FakeLLM)


def test_get_llm_reads_settings_not_environ(monkeypatch):
    # 1.R.2: the factory must read app.core.config settings (like every other
    # service), not os.environ directly — env alone must not decide the backend.
    monkeypatch.setenv("LLM_BACKEND", "fake")
    monkeypatch.setenv("OLLAMA_MODEL", "env-model")
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "ollama_model", "settings-model")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama-host:11434")
    llm = get_llm()
    assert isinstance(llm, OllamaLLM)
    assert llm._model == "settings-model"
    assert llm._base_url == "http://ollama-host:11434"


async def test_llm_complete_accepts_system_prompt():
    llm = FakeLLM()
    result = await llm.complete("What is Python?", system="You are a helpful assistant.")
    assert isinstance(result, str)
```

- [x] **1.2** Implement: *([plan-fix]: `requests` is not a backend dependency — OllamaLLM uses `httpx` (moved to runtime deps); OpenAIAdapter fully typed and `openai.*` mypy override added so `mypy --strict app/services/` passes; [1.R]: async adapters via `httpx.AsyncClient`/`AsyncOpenAI`, settings-driven factory, adapters raise instead of stubbing)*

```python
# backend/app/services/llm_service.py
"""
LLM service adapter — on-prem first (ADR-010).

Feature flag:
  LLM_ALLOW_EXTERNAL=false (default) → only local backends (Ollama, fake)
  LLM_ALLOW_EXTERNAL=true            → external APIs allowed (OpenAI)

Backends:
  LLM_BACKEND=fake            → FakeLLM (tests + local dev without Ollama)
  LLM_BACKEND=ollama          → Ollama local server
  LLM_BACKEND=openai          → OpenAI API (requires LLM_ALLOW_EXTERNAL=true)

Adapters are async (the /ask path runs on the event loop) and RAISE on backend
failure — they never fabricate answer text. The ADR-010 graceful-degradation
path (ranked sources without synthesis) is owned by rag_service.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from app.core.config import settings


@runtime_checkable
class LLMAdapter(Protocol):
    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        """Send a prompt and return the text completion. Raises on backend failure."""
        ...


class FakeLLM:
    """
    Deterministic fake for tests and local dev without an LLM backend.
    Returns a templated string so tests can assert on structure.
    """

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        return f"[FakeLLM response to: {prompt[:80]}]"


class OllamaLLM:
    """
    Adapter for a local Ollama server (async HTTP via httpx.AsyncClient —
    a sync client here would block the event loop for the full 60s timeout).
    Failures propagate; rag_service owns the degrade path (ADR-010).
    """

    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
    ) -> None:
        self._model = model
        self._base_url = base_url

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self._base_url}/api/generate", json=payload)
        r.raise_for_status()
        response = r.json().get("response", "")
        return response if isinstance(response, str) else ""


def get_llm() -> LLMAdapter:
    """
    Factory respecting the LLM_ALLOW_EXTERNAL feature flag.
    If LLM_ALLOW_EXTERNAL=false, external backends are silently downgraded to FakeLLM.
    Config comes from app.core.config settings (env var names unchanged via
    pydantic-settings) — never from os.environ directly.
    """
    if settings.llm_backend == "fake":
        return FakeLLM()

    if settings.llm_backend == "ollama":
        return OllamaLLM(model=settings.ollama_model, base_url=settings.ollama_base_url)

    if settings.llm_backend == "openai":
        if not settings.llm_allow_external:
            # ADR-010: external APIs blocked by default
            return FakeLLM()
        # openai backend (optional dep)
        try:
            from openai import AsyncOpenAI

            class OpenAIAdapter:
                def __init__(self) -> None:
                    self._client = AsyncOpenAI()
                    self._model = settings.openai_model

                async def complete(
                    self, prompt: str, *, system: str = "", max_tokens: int = 512
                ) -> str:
                    msgs: list[dict[str, str]] = []
                    if system:
                        msgs.append({"role": "system", "content": system})
                    msgs.append({"role": "user", "content": prompt})
                    r: Any = await self._client.chat.completions.create(
                        model=self._model, messages=msgs, max_tokens=max_tokens
                    )
                    content = r.choices[0].message.content
                    return content if isinstance(content, str) else ""

            return OpenAIAdapter()
        except ImportError:
            return FakeLLM()

    return FakeLLM()
```

- [x] **1.3** Add to `config.py` (also in pyproject: `httpx>=0.27` moved dev → runtime deps; `[[tool.mypy.overrides]] module = "openai.*"` added; [1.R]: `openai_model` added — these fields are now actually consumed by `get_llm()`):
```python
llm_backend: str = "ollama"  # fake | ollama | openai
llm_allow_external: bool = False
ollama_model: str = "llama3"
ollama_base_url: str = "http://localhost:11434"
openai_model: str = "gpt-4o-mini"
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

### 1.R Review fixes (post-commit 22d0423)

- [x] **1.R.1** (CRITICAL) `complete()` was a sync `httpx.post` called inside the async `/ask`
  path — it blocked the event loop for up to 60s. `LLMAdapter.complete` is now `async def`;
  OllamaLLM uses `httpx.AsyncClient`, FakeLLM is async, OpenAIAdapter uses `AsyncOpenAI`.
  Test: `test_adapter_complete_is_async`.
- [x] **1.R.2** (IMPORTANT) `get_llm()` read `os.environ` directly, leaving the four Settings
  fields dead. The factory now reads `app.core.config.settings` like every other service
  (env var names unchanged via pydantic-settings); `openai_model` added to Settings.
  Test: `test_get_llm_reads_settings_not_environ`. `tests/conftest.py` autouse fixture
  (`_fake_ai_backends`) now also forces `settings.llm_backend = "fake"` so API tests never
  touch a live Ollama.
- [x] **1.R.3** OllamaLLM no longer swallows exceptions into a `[LLM unavailable: {exc}]` stub —
  that leaked internal exception text into answer bodies. Adapters raise; the ADR-010 degrade
  path lives in rag_service (see 2.R.1).

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
    assert result.degraded is False


class _ExplodingLLM:
    """Adapter that fails like an unreachable backend would (2.R.1)."""

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
        raise RuntimeError("boom-internal-detail")


async def test_rag_degrades_without_llm(db, make_user, make_node, fake_embedder):
    """2.R.1 (ADR-010): on LLM failure, return ranked sources WITHOUT synthesis —
    answer is None, degraded is True, and no internal exception detail leaks."""
    owner = await make_user(email="rag_d1@test.com")
    node = await make_node(owner, title="Python Guide",
                          body="Python is great for data science and ML pipelines.",
                          visibility=Visibility.public)
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    result = await rag.ask(
        db, "What is Python good for?", viewer,
        embedder=fake_embedder, llm=_ExplodingLLM(),
    )
    assert result.answer is None
    assert result.degraded is True
    assert str(node.id) in [s["id"] for s in result.sources]
    assert "boom-internal-detail" not in repr(result)


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

import logging
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


logger = logging.getLogger(__name__)


@dataclass
class RAGResult:
    """RAG outcome. `answer is None` + `degraded=True` means the LLM was
    unavailable and the caller gets ranked sources without synthesis (ADR-010)."""

    answer: str | None
    sources: list[dict[str, Any]]
    query: str
    degraded: bool = False


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
    results, _ = await ss.hybrid_search(db, query, viewer, limit=limit, embedder_override=embedder)

    if not results:
        return RAGResult(answer=_NO_CONTEXT_ANSWER, sources=[], query=query)

    # Step 2: build context string.
    # SECURITY: retrieved node bodies are UNTRUSTED input to the prompt — any
    # user (or ingested Confluence page / scanned repo) can write text that
    # tries to override the system prompt (prompt injection). Never treat
    # retrieved content as instructions when extending the prompt format.
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

    # Step 3: LLM completion. On any backend failure, degrade per ADR-010:
    # ranked sources WITHOUT synthesis. The exception is logged server-side
    # only — raw exception text must NEVER reach the caller.
    try:
        answer = await llm.complete(prompt, system=_SYSTEM_PROMPT)
    except Exception:
        logger.exception("llm_completion_failed — degrading to retrieval-only response")
        return RAGResult(answer=None, sources=results[:limit], query=query, degraded=True)

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
    assert data["degraded"] is False


async def test_ask_requires_auth(client: AsyncClient):
    r = await client.post("/api/v1/ask", json={"query": "test"})
    assert r.status_code == 401


async def test_ask_degrades_on_llm_failure(client: AsyncClient, auth_headers, monkeypatch):
    """2.R.1 (ADR-010): LLM down → 200 with ranked sources, answer null,
    degraded true — and never any raw exception text in the body."""
    import app.api.v1.ask as ask_module

    class _ExplodingLLM:
        async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 512) -> str:
            raise RuntimeError("boom-internal-detail")

    monkeypatch.setattr(ask_module, "get_llm", lambda: _ExplodingLLM())

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
    assert data["answer"] is None
    assert data["degraded"] is True
    assert len(data["sources"]) >= 1
    assert "boom-internal-detail" not in r.text
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
    """`limit` caps how many retrieved nodes feed the RAG context. The `le=20`
    bound intentionally deviates from the kb-api-conventions pagination cap
    (100): it bounds prompt/context size, not a page size."""

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(5, ge=1, le=20)


class AskResponse(BaseModel):
    """`answer: null` + `degraded: true` = LLM unavailable; `sources` still
    carries the ranked retrieval results (ADR-010 graceful degradation)."""

    answer: str | None
    sources: list[dict[str, Any]]
    query: str
    degraded: bool = False


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
    return AskResponse(
        answer=result.answer,
        sources=result.sources,
        query=result.query,
        degraded=result.degraded,
    )
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
# After review fixes (1.R/2.R): 6 passed in these files; full suite 218 passed
```

### 2.R Review fixes (post-commit 3ffe27c)

- [x] **2.R.1** (CRITICAL, ADR-010 degrade shape) On LLM failure/unavailability, /ask now
  returns HTTP 200 with the ranked sources and NO synthesis: `answer: null` +
  `degraded: true` (smallest schema addition). Raw exception text is logged server-side and
  NEVER surfaced to the caller (the old OllamaLLM stub embedded `{exc}` in the answer).
  Tests: `test_rag_degrades_without_llm`, `test_ask_degrades_on_llm_failure`.
- [x] **2.R.2** rag_service now `await`s `llm.complete(...)` — adapters are async (1.R.1).
- [x] **2.R.3** hybrid_search's `fake_embedder` kwarg renamed to `embedder_override`: rag_service
  injects it on a production path, so the name lied. All call sites, search tests and the
  phase-2 plan snippets updated.
- [x] **2.R.4** Comment added in rag_service: retrieved node bodies are UNTRUSTED input to the
  prompt (prompt-injection surface) — never treat them as instructions when extending the
  prompt format.
- [x] **2.R.5** AskRequest docstring documents that `limit ≤ 20` intentionally deviates from the
  kb-api-conventions pagination cap (100): it bounds RAG context size, not a page size.

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

- [x] **3.1** Write failing test:

```python
# backend/tests/services/test_audit_service.py
import pytest

from app.models.user import Role
from app.services import audit_service as audit
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_log_action(db, make_user):
    owner = await make_user(email="audit1@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await audit.log(
        db,
        viewer=viewer,
        action="node.create",
        resource_type="node",
        resource_id=str(owner.id),
        meta={"title": "Test"},
    )
    await db.flush()
    from sqlalchemy import select

    from app.models.audit import AuditLog

    rows = await db.scalars(select(AuditLog).where(AuditLog.actor_id == owner.id))
    entries = list(rows)
    assert len(entries) == 1
    assert entries[0].action == "node.create"
```

- [x] **3.2** Create model (also export `AuditLog` from `app/models/__init__.py`): *([plan-fix]: plan named the table `audit_logs`; kb-conventions canonical vocabulary and the phase-1 Task 8 carry-over say `audit_log` — renamed in model + migration)*

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
    # [plan-fix] plan named the table "audit_logs"; kb-conventions canonical
    # vocabulary (and the phase-1 Task 8 carry-over) say "audit_log" — kept canonical.
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)  # "node.create" etc.
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(256))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_audit_actor", "actor_id"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_created_at", "created_at"),
    )
```

- [x] **3.3** Create audit service:

```python
# backend/app/services/audit_service.py
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.services.visibility import Viewer

_META_MAX_BYTES = 4096  # [3.R.1] cap serialized meta; audit rows must stay small


def _cap_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Bound `meta` to ~4KB serialized [3.R.1]. Oversized payloads → marker dict
    with a UTF-8-safe preview (audit trail, not a payload store)."""
    if not meta:
        return {}
    serialized = json.dumps(meta, default=str)
    if len(serialized.encode("utf-8")) <= _META_MAX_BYTES:
        return meta
    preview = serialized.encode("utf-8")[: _META_MAX_BYTES - 64].decode("utf-8", errors="ignore")
    return {"truncated": True, "preview": preview}


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
    """Append an audit entry (committed with the caller's transaction).

    [3.R.1] `meta` is stored verbatim in JSONB and surfaced on admin dashboards:
    do NOT put secrets or PII in it — Task 6 admin reads pass cross-user data
    through here, so keep meta to ids/titles/counts. >~4KB → truncation marker.
    """
    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=viewer.user_id if viewer else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        meta=_cap_meta(meta),
    )
    db.add(entry)
    # Non-blocking: do not flush here — caller's transaction commits it
```

- [x] **3.4** Migration `0006_audit_log.py`: *([plan-fix]: table renamed `audit_logs` → `audit_log` (canonical); header matches the typed house style of 0005)*

```python
# backend/alembic/versions/0006_audit_log.py
"""audit_log table

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [plan-fix] plan named the table "audit_logs"; canonical name is "audit_log".
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(256)),
        sa.Column("ip_address", sa.String(64)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_actor", "audit_log", ["actor_id"])
    op.create_index("ix_audit_action", "audit_log", ["action"])
    op.create_index("ix_audit_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
```

- [x] **3.5** Apply and run tests:
```bash
cd backend && alembic upgrade head
pytest tests/services/test_audit_service.py -v
# Expected: 1 passed
```

- [x] **3.6** Commit:
```
feat(audit): AuditLog model + migration 0006 + audit_service.log()
```

### 3.R Review fixes (post-commit 6d91af5)

- [x] **3.R.1** (NIT) `audit_service.log()` now caps `meta`: JSON-serialized payloads over
  4KB are replaced with `{"truncated": True, "preview": <first ~4KB>}` (`_cap_meta`), and
  the docstring warns against putting secrets/PII in `meta` — Task 6 admin reads will pass
  cross-user data through this path. Tested: oversized meta gets the marker + stays ≤ ~4KB;
  small meta is stored verbatim.

> **Carry-over from phase-1 Task 8 (now actionable):** the audit mechanism exists as of
> this task, but nothing calls it yet. Task 6 (admin API) MUST call
> `audit_service.log()` on every `/api/v1/admin/*` read that exposes another user's
> non-public data (ADR-004 / kb-visibility-filter rule 5) — the Task 6 code blocks as
> written do not do this and must be extended when Task 6 is executed.

---

## Task 4 — JWT refresh revocation

**Files:**
- Modify: `backend/app/core/security.py`
- Modify: `backend/app/api/v1/auth.py`
- Create: `backend/tests/api/test_token_revocation.py`

### Steps

- [x] **4.1** Write failing test: *([plan-fix]: no `/auth/register` endpoint and login is JSON, not form-data (same as conftest [plan-fix, Task 8.5]) — register via `auth_service`; `pytestmark` dropped, asyncio_mode is auto)*

```python
# backend/tests/api/test_token_revocation.py
from app.services import auth_service


async def test_refresh_token_used_twice_rejected(db, client) -> None:
    """Using a refresh token a second time must return 401 (rotation + revocation)."""
    await auth_service.register(db, email="revoke@test.com", password="pass1234", display_name="R")
    r = await client.post(
        "/api/v1/auth/login", json={"email": "revoke@test.com", "password": "pass1234"}
    )
    refresh_token = r.json()["refresh_token"]

    # First refresh — should succeed
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r1.status_code == 200

    # Second use of same refresh token — must be rejected
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r2.status_code == 401
```

- [x] **4.2** Implement revocation list in Redis: *([plan-fix]: client memoized per running event loop — a forever-cached global client is bound to the loop it was created on and raises "Event loop is closed" after any loop change (one loop per test); `setex` is deprecated → `set(ex=)`)*

```python
# backend/app/core/security.py  (add to existing file)
import asyncio
import redis.asyncio as aioredis
from app.core.config import settings

_redis: aioredis.Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not loop:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        _redis_loop = loop
    return _redis


async def claim_jti_once(jti: str, ttl_seconds: int) -> bool:
    """Atomically claim a refresh JTI (ADR-008: single-use tokens).

    [4.R.1] Single SET NX EX — Redis serializes concurrent claims, so exactly
    one caller gets True; every other caller (token reuse, incl. races) gets
    False. Replaces the non-atomic is_jti_revoked/revoke_jti check-then-set.
    TTL is clamped to >= 1s so a token in its final second still burns its JTI.
    """
    r = await _get_redis()
    return bool(await r.set(f"revoked_jti:{jti}", "1", nx=True, ex=max(ttl_seconds, 1)))
```

- [x] **4.3** Update refresh endpoint in `auth.py` to revoke old JTI on use: *([plan-fix]: canonical names — existing endpoint is `refresh` with `RefreshIn` / `make_access_token(user_id, role)` / `make_refresh_token`, not `RefreshRequest`/`create_*`; raw `db.scalar(select(User)...)` in the router violates kb-api-conventions "no DB queries in routers" → moved to new `auth_service.get_active_user(db, user_id)`; catch `pyjwt.PyJWTError`, not bare `Exception`)*

```python
# backend/app/api/v1/auth.py — refresh endpoint (imports at module top, not inline)
@router.post(
    "/refresh", response_model=TokensOut, summary="Rotate tokens", operation_id="refreshTokens"
)
async def refresh(payload: RefreshIn, db: AsyncSession = Depends(get_db)) -> TokensOut:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid refresh token") from exc

    # ADR-008: rotation + revocation — a refresh token is single-use.
    # [4.R.1] Single atomic claim (SET NX EX); [4.R.2] Redis down → 503 fail closed.
    jti = claims.get("jti")
    if jti:
        remaining = max(int(claims["exp"] - time.time()), 1)
        try:
            claimed = await claim_jti_once(jti, remaining)
        except (RedisError, OSError) as exc:
            raise HTTPException(
                status_code=503, detail="service temporarily unavailable"
            ) from exc
        if not claimed:
            raise HTTPException(status_code=401, detail="refresh token reused")

    user = await auth_service.get_active_user(db, uuid.UUID(claims["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")  # auth boundary

    return TokensOut(
        access_token=make_access_token(user.id, user.role.value),
        refresh_token=make_refresh_token(user.id, user.role.value),
    )
```

- [x] **4.4** Run tests:
```bash
cd backend && pytest tests/api/test_token_revocation.py -v
# Expected: 1 passed  → actual: 1 passed; full tests/api/: 92 passed, 3 skipped
```

- [x] **4.5** Commit:
```
feat(auth): JWT refresh token revocation via Redis JTI blocklist
```

### 4.R Review fixes (post-commit ea2d459)

- [x] **4.R.1** (CRITICAL) Refresh rotation race: `is_jti_revoked` + `revoke_jti` was a
  non-atomic check-then-set — two concurrent refreshes with the same token could both
  pass the check and both rotate. Replaced with a single atomic claim,
  `claim_jti_once(jti, ttl)` = Redis `SET revoked_jti:{jti} 1 NX EX <ttl>` (ttl =
  remaining refresh lifetime from `exp`, clamped >= 1s); NX failure → 401
  "refresh token reused". Old helpers deleted (no other callers). Tests: a
  deterministic race (barrier proxy holds `exists` until both racers have read —
  correct NX code never calls `exists`) asserts exactly one 200 + one 401, and a
  pre-claimed-key test documents the NX semantics.
- [x] **4.R.2** (IMPORTANT) Redis-down behavior on refresh is now explicit: `RedisError`/
  `OSError` from the claim → 503 with generic detail "service temporarily unavailable"
  (fail CLOSED — rotating tokens without enforcing single-use would reopen replay).
  Tested via a monkeypatched redis client whose commands raise `ConnectionError`.

> **Known gap (carried from phase-3 `## Blockers`):** the frontend BFF still has no
> `/api/auth/refresh` route handler, so the browser never exercises this endpoint —
> backend rotation + revocation is live for direct API/CLI clients only. BFF wiring
> is out of scope for this task and remains open.

---

## Task 5 — Rate limiting middleware

**Files:**
- Create: `backend/app/core/rate_limit.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/api/test_rate_limit.py`

### Steps

- [x] **5.1** Write failing test:

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

- [x] **5.2** Implement sliding-window rate limiter using Redis: *([plan-fix]: the
  original block raised `HTTPException` inside the middleware, but
  `BaseHTTPMiddleware` runs OUTSIDE FastAPI's `ExceptionMiddleware`, so the
  exception is never converted to a 429 — it surfaces as a server error.
  Return a `JSONResponse` directly instead; on Redis failure, fail open.
  Also hoisted the inline `hashlib`/`_get_redis` imports to module top and
  typed `call_next` for mypy.)*

```python
# backend/app/core/rate_limit.py
"""
Redis sliding-window rate limiter.
Applied per user_id to expensive endpoints (/ask, /search).
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Awaitable, Callable

import jwt as pyjwt
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.security import ALGO, _get_redis

_LIMITS: dict[str, tuple[int, int]] = {
    "/api/v1/ask": (20, 60),  # 20 requests per 60 seconds
    "/api/v1/search": (60, 60),  # 60 requests per 60 seconds
}


def _limit_for(path: str) -> tuple[int, int] | None:
    """Segment-bound match [5.R.3]: '/api/v1/ask' and '/api/v1/ask/...' are
    limited; an unrelated sibling like '/api/v1/askew' is not."""
    for prefix, config in _LIMITS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return config
    return None


def _bucket_key(auth_header: str) -> str:
    """Rate-limit identity [5.R.1]: the token's `sub` claim (user id), so every
    token a user holds draws from ONE bucket — keying on the raw header would
    hand out a fresh budget per login. Invalid/undecodable token → hash of the
    header (the request is still limited; auth rejects it downstream anyway).
    """
    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = pyjwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
        sub = claims.get("sub")
        if sub:
            return f"sub:{sub}"
    except pyjwt.PyJWTError:
        pass
    return "tok:" + hashlib.sha256(auth_header.encode()).hexdigest()[:16]


async def rate_limit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    limit_config = _limit_for(path)
    if limit_config is None:
        return await call_next(request)

    max_requests, window_seconds = limit_config

    # Get user identity from JWT (if present)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await call_next(request)

    redis_key = f"rate:{path}:{_bucket_key(auth)}"

    try:
        r = await _get_redis()
        now = time.time()
        window_start = now - window_seconds

        # Sliding window using sorted set
        pipe = r.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zadd(redis_key, {str(now): now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        pipe.zrange(redis_key, 0, 0, withscores=True)  # oldest entry, for Retry-After
        results = await pipe.execute()
        count: int = results[2]
        oldest: list[tuple[str, float]] = results[4]
    except Exception:
        # Redis unavailable — fail open (don't block legitimate requests)
        return await call_next(request)

    if count > max_requests:
        # [plan-fix]: the plan raised HTTPException here, but BaseHTTPMiddleware
        # runs OUTSIDE FastAPI's ExceptionMiddleware, so the exception would
        # never be converted to a 429 response (it surfaces as a server error).
        # Return the JSON response directly instead.
        # [5.R.2] Retry-After = when the oldest window entry ages out (a slot
        # frees then), not a hardcoded window; bounded to [1, window].
        retry_after = window_seconds
        if oldest:
            frees_in = math.ceil(oldest[0][1] + window_seconds - now)
            retry_after = min(max(frees_in, 1), window_seconds)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": f"Rate limit exceeded: {max_requests} requests per {window_seconds}s"
            },
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)
```

- [x] **5.3** Register middleware in `main.py` (imports at module top per
  project style; only `_LIMITS` paths are limited, so `/healthz` is exempt,
  and `BaseHTTPMiddleware` ignores non-http scopes so WebSockets pass through):

```python
# backend/app/main.py (add in create_app)
from app.core.rate_limit import rate_limit_middleware
from starlette.middleware.base import BaseHTTPMiddleware
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
```

- [x] **5.4** Run tests:
```bash
cd backend && pytest tests/api/test_rate_limit.py -v
# Expected: 1 passed
```

- [x] **5.5** Commit:
```
feat(api): Redis sliding-window rate limiting on /ask and /search (20/60s, 60/60s)
```

### 5.R Review fixes (post-commit e48d247)

- [x] **5.R.1** (IMPORTANT) Bucket key is now the decoded `sub` claim (user id) — keying on
  `sha256(Authorization)` gave every freshly minted token its own budget, so a user could
  dodge the limit by logging in again. Undecodable/invalid token → fall back to the header
  hash (still limited). Test: two tokens for the same user share one bucket (15 requests
  each trips the 20/60s limit → 429).
- [x] **5.R.2** (IMPORTANT) `Retry-After` is computed from the sliding-window zset's oldest
  entry — `ceil(oldest + window - now)`, clamped to `[1, window]` — instead of hardcoded 60.
  The zset `zrange(0, 0, withscores=True)` rides the existing pipeline (no extra roundtrip).
  Test seeds a full bucket 50s old and asserts Retry-After ≈ 10.
- [x] **5.R.3** (NIT) Path matching is segment-bound via `_limit_for()` —
  `path == p or path.startswith(p + "/")` — so `/api/v1/askew` is not captured by the
  `/api/v1/ask` limit. Unit-tested directly.

---

## Task 6 — Admin API + dashboards

> **Carry-over from phase-1 Task 8 (via Task 3):** every `/api/v1/admin/*` read that
> exposes another user's non-public data must write an audit entry via
> `audit_service.log()` (ADR-004 / kb-visibility-filter rule 5). The code blocks below
> predate the audit service and do not include these calls — add them (plus tests)
> when executing this task.

**Files:**
- Create: `backend/app/api/v1/admin/stats.py` *([plan-fix]: admin is a package since phase-1 Task 8, not a single `admin.py`)*
- Create: `backend/app/api/v1/admin/audit_logs.py`
- Modify: `backend/app/api/v1/admin/__init__.py`
- Create: `frontend/src/app/admin/page.tsx`
- Modify: `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` *(typed client — kb-conventions forbid raw fetch)*
- Create: `backend/tests/api/test_admin_api.py`

### Steps

- [x] **6.1** Write failing tests: *([plan-fix]: no seeded admin / form login exists — JSON login + `auth_service.register` like every API test; `admin@kb.local` fails EmailStr (`.local` reserved) → `admin@kb.example`; added 401 case, filter/pagination cases, and the carry-over audit-of-admin-read tests)*

```python
# backend/tests/api/test_admin_api.py
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.user import Role
from app.services import auth_service


async def _admin_headers(db, client) -> tuple[dict[str, str], object]:
    admin = await auth_service.register(
        db, email="admin@kb.example", password="admin1234", display_name="Admin", role=Role.admin
    )
    r = await client.post(
        "/api/v1/auth/login", json={"email": "admin@kb.example", "password": "admin1234"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, admin


async def test_admin_stats_requires_auth(client) -> None:
    r = await client.get("/api/v1/admin/stats")
    assert r.status_code == 401


async def test_admin_stats_requires_admin(client, auth_headers) -> None:
    r = await client.get("/api/v1/admin/stats", headers=auth_headers)
    assert r.status_code == 403


async def test_admin_stats_for_admin(db, client) -> None:
    headers, _ = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/stats", headers=headers)
    assert r.status_code == 200
    data = r.json()
    for key in ("total_users", "active_users", "total_nodes", "total_chunks", "total_audit_events"):
        assert key in data
    assert data["total_users"] >= 1


async def test_admin_stats_excludes_soft_deleted_nodes(db, client, make_user, make_node) -> None:
    ...  # soft-deleted node not counted in total_nodes (see test file)


async def test_admin_audit_log(db, client) -> None:
    headers, _ = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/audit-logs", headers=headers)
    assert r.status_code == 200
    assert "items" in r.json() and "total" in r.json()


async def test_admin_audit_logs_requires_admin(client, auth_headers) -> None:
    r = await client.get("/api/v1/admin/audit-logs", headers=auth_headers)
    assert r.status_code == 403


async def test_admin_audit_logs_filter_and_pagination(db, client, make_user) -> None:
    ...  # ?action= filters items; ?limit=101 → 422 (shared Pagination caps at 100)


# Carry-over (phase-1 Task 8 via Task 3): admin reads of cross-user,
# non-public data must themselves be audit-logged (kb-visibility-filter rule 5).

async def test_admin_stats_read_is_audited(db, client) -> None:
    headers, admin = await _admin_headers(db, client)
    assert (await client.get("/api/v1/admin/stats", headers=headers)).status_code == 200
    rows = await db.scalars(
        select(AuditLog).where(AuditLog.actor_id == admin.id, AuditLog.action == "admin.stats.read")
    )
    assert len(list(rows)) == 1


async def test_admin_audit_logs_read_is_audited(db, client) -> None:
    headers, admin = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/audit-logs?action=node.create", headers=headers)
    assert r.status_code == 200
    rows = await db.scalars(
        select(AuditLog).where(
            AuditLog.actor_id == admin.id, AuditLog.action == "admin.audit_logs.read"
        )
    )
    entries = list(rows)
    assert len(entries) == 1
    assert entries[0].meta.get("action_filter") == "node.create"
```

- [x] **6.2** Create admin router modules: *([plan-fix]: split into the existing admin package (`stats.py`, `audit_logs.py`, registered in `admin/__init__.py`); the plan's `/users` endpoint was dropped — `GET /admin/users` (adminListUsers, proper `UserOut`) has existed since phase-1 Task 8; carry-over honored: both reads call `audit_service.log()` with the full-role viewer; `total` on /audit-logs now respects the `action` filter; limit ≤ 100 via shared `Pagination` dep, not `le=200`)*

```python
# backend/app/api/v1/admin/stats.py  (require_admin applied at package include)
router = APIRouter(tags=["admin"])


class StatsOut(BaseModel):
    total_users: int
    active_users: int
    total_nodes: int
    total_chunks: int
    total_audit_events: int


@router.get("/stats", response_model=StatsOut, summary="Admin dashboard stats",
            operation_id="adminGetStats")
async def get_stats(
    viewer: Viewer = Depends(get_current_viewer),  # full-role: audited admin exception
    db: AsyncSession = Depends(get_db),
) -> StatsOut:
    total_users = await db.scalar(select(func.count()).select_from(User)) or 0
    active_users = (
        await db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True))) or 0
    )
    total_nodes = (
        await db.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.deleted_at.is_(None))
        )
        or 0
    )
    total_chunks = await db.scalar(select(func.count()).select_from(NodeChunk)) or 0
    total_audit = await db.scalar(select(func.count()).select_from(AuditLog)) or 0
    stats = StatsOut(total_users=total_users, active_users=active_users,
                     total_nodes=total_nodes, total_chunks=total_chunks,
                     total_audit_events=total_audit)
    await audit_service.log(db, viewer=viewer, action="admin.stats.read",
                            resource_type="stats", meta=stats.model_dump())
    return stats
```

```python
# backend/app/api/v1/admin/audit_logs.py
router = APIRouter(tags=["admin"])


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    created_at: datetime
    meta: dict[str, Any]

    model_config = {"from_attributes": True}


class AuditLogsListOut(BaseModel):
    items: list[AuditLogOut]
    total: int


@router.get("/audit-logs", response_model=AuditLogsListOut,
            summary="List audit log entries", operation_id="adminListAuditLogs")
async def list_audit_logs(
    action: str | None = Query(None, max_length=128),
    page: Pagination = Depends(),
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
) -> AuditLogsListOut:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    count_stmt = select(func.count()).select_from(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    total = await db.scalar(count_stmt) or 0
    rows = await db.scalars(stmt.offset(page.offset).limit(page.limit))
    items = [AuditLogOut.model_validate(r) for r in rows]
    await audit_service.log(db, viewer=viewer, action="admin.audit_logs.read",
                            resource_type="audit_log",
                            meta={"action_filter": action, "offset": page.offset,
                                  "limit": page.limit, "returned": len(items)})
    return AuditLogsListOut(items=items, total=total)
```

- [x] **6.3** ~~Add `require_admin` dep~~ *([plan-fix]: already exists in `app/core/deps.py` since phase-1 (raises 403 for non-admin, returns the full-role `Viewer`) — no change needed)*

- [x] **6.4** ~~Register in `main.py`~~ *([plan-fix]: the admin package router is already registered in `create_app()` since phase-1 — the new modules are included via `admin/__init__.py` instead)*

- [x] **6.5** Create admin frontend page: *([plan-fix]: plan's raw `fetch()` violates kb-conventions/ADR-013 — added `AdminStats` to `lib/types.ts` and `fetchAdminStats()` to the typed client `lib/api.ts`, with vitest coverage (api client + page module))*

```typescript
// frontend/src/app/admin/page.tsx
"use client"
import { useQuery } from "@tanstack/react-query"
import Sidebar from "@/components/Sidebar"
import { fetchAdminStats } from "@/lib/api"

export default function AdminPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin-stats"],
    queryFn: fetchAdminStats,
  })

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

- [x] **6.6** Run tests:
```bash
cd backend && pytest tests/api/test_admin_api.py -v
# Actual: 9 passed ([plan-fix]: 3 planned + 401, soft-delete count, filter/
# pagination, requires-admin on audit-logs, and 2 carry-over audit tests)
cd frontend && npx vitest run && npx tsc --noEmit
# Actual: 23 passed (7 files), tsc clean
```

- [x] **6.7** Commit:
```
feat(admin): GET /api/v1/admin/stats + /audit-logs with audited admin reads   (8003477)
feat(admin): dashboard page + typed fetchAdminStats client
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
