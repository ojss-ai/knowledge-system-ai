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
