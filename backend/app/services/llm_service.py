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
