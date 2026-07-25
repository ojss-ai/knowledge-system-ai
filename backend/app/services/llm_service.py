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
