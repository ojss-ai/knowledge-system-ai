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
