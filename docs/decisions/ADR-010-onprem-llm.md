# ADR-010: On-prem LLM (Ollama) behind a feature flag

**Status:** Accepted · 2026-06-12

## Context
RAG ask-mode, auto-tag suggestions, and code summaries want an LLM. Private knowledge must not leave company infrastructure by default; Confluence and codebase content is confidential.

## Decision
LLM access goes through one adapter (`app/services/llm_service.py`) speaking the OpenAI-compatible API. Default backend: Ollama (or vLLM) on-prem. External APIs (Anthropic/OpenAI) are a deployment-level opt-in flag (`LLM_ALLOW_EXTERNAL=false` by default). Every LLM feature degrades gracefully when disabled: `/ask` returns ranked sources without synthesis; scanner skips summaries; tagging falls back to embedding similarity.

## Consequences
- Privacy by default; one place to audit what text reaches the model.
- On-prem model quality < frontier APIs — prompts must be robust to weaker models; citations always come from retrieval, never model memory.
- GPU capacity planning enters ops scope only if summaries/ask-mode see heavy use.

## Revisit when
Company approves an external provider under DPA, or local model quality stops being sufficient.
