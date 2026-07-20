"""Edges router — edges live only in Neo4j (ADR-011).

Both handlers re-check node visibility through node_service.get_node first,
so an invisible endpoint node looks nonexistent (404, generic body) and no
edge can be attached to a node the caller cannot see (kb-visibility-filter).
Mutating an edge additionally requires OWNING the source node — same standard
as node mutations; a visible-but-not-owned source is 403 (the node is visible,
so a 403 confirms nothing). Wikilink-generated edges are unaffected: they run
as the owner inside node_service. All checks fire BEFORE any Neo4j call.
There is no PG write here, hence no commit; the Neo4j MERGE/DELETE runs
directly (a down Neo4j surfaces as 503 via the central error mapping).
Viewer comes from get_scoped_viewer — no admin bypass outside /api/v1/admin/*
(admin edge mutations, when needed, belong on audited /api/v1/admin/* routes).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer
from app.core.errors import ForbiddenError
from app.schemas.node import EdgeCreate, EdgeDelete, EdgeOut
from app.services import graph_service as gs
from app.services import node_service as ns

router = APIRouter(prefix="/edges", tags=["edges"])


async def _check_endpoints(
    db: AsyncSession, viewer: Viewer, source_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """Shared create/delete gate, in this order:

    1. Source visible, else 404 (invisible == nonexistent, ADR-004).
    2. Source OWNED by the viewer, else 403 — edge mutations are owner-only,
       mirroring node mutations. Safe to say 403: the node is visible.
    3. Target visible, else 404.
    """
    source = await ns.get_node(db, source_id, viewer)
    if source.owner_id != viewer.user_id:
        raise ForbiddenError("Only the source node owner can modify its edges")
    await ns.get_node(db, target_id, viewer)


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
    await _check_endpoints(db, viewer, payload.source_id, payload.target_id)
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
    # Same gate as create_edge: both endpoints visible, source owned.
    await _check_endpoints(db, viewer, payload.source_id, payload.target_id)
    await gs.delete_edge(payload.source_id, payload.target_id, payload.label)
