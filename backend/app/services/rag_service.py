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
