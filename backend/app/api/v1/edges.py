"""Edges router — edges live only in Neo4j (ADR-011).

Both handlers re-check node visibility through node_service.get_node first,
so an invisible endpoint node looks nonexistent (404, generic body) and no
edge can be attached to a node the caller cannot see (kb-visibility-filter).
There is no PG write here, hence no commit; the Neo4j MERGE/DELETE runs
directly (a down Neo4j surfaces as 503 via the central error mapping).
Viewer comes from get_scoped_viewer — no admin bypass outside /api/v1/admin/*.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer
from app.schemas.node import EdgeCreate, EdgeDelete, EdgeOut
from app.services import graph_service as gs
from app.services import node_service as ns

router = APIRouter(prefix="/edges", tags=["edges"])


@router.post(
    "",
    response_model=EdgeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create edge",
    operation_id="createEdge",
)
async def create_edge(
    payload: EdgeCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> EdgeOut:
    # Both endpoints must be visible to the viewer (invisible == nonexistent).
    await ns.get_node(db, payload.source_id, viewer)
    await ns.get_node(db, payload.target_id, viewer)
    await gs.merge_edge(
        payload.source_id, payload.target_id, payload.label, created_by=str(viewer.user_id)
    )
    return EdgeOut(source_id=payload.source_id, target_id=payload.target_id, label=payload.label)


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete edge",
    operation_id="deleteEdge",
)
async def delete_edge(
    payload: EdgeDelete,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> None:
    # Both endpoints must be visible, symmetric with create_edge — an invisible
    # target must look nonexistent (404) before any Neo4j call (ADR-004).
    await ns.get_node(db, payload.source_id, viewer)
    await ns.get_node(db, payload.target_id, viewer)
    await gs.delete_edge(payload.source_id, payload.target_id, payload.label)
