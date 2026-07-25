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
