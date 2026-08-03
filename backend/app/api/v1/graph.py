"""Graph read router — neighborhood traversal and overview viewport.

An invisible center node is indistinguishable from a nonexistent one: the
PG visibility check (node_service.get_node) runs BEFORE any Neo4j traversal
and raises NotFoundError → 404 with a generic body (kb-visibility-filter).
Traversal results are themselves visibility-filtered inside graph_service
via a PG re-check, so an incomplete-looking neighborhood is correct behavior.
Viewer comes from get_scoped_viewer — no admin bypass outside /api/v1/admin/*.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer
from app.schemas.node import GraphNeighborhoodOut
from app.services import graph_service as gs
from app.services import node_service as ns

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get(
    "/neighborhood/{node_id}",
    response_model=GraphNeighborhoodOut,
    summary="Get node neighborhood",
    operation_id="getGraphNeighborhood",
)
async def get_neighborhood(
    node_id: uuid.UUID,
    hops: int = Query(1, ge=0, le=3),
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> GraphNeighborhoodOut:
    await ns.get_node(db, node_id, viewer)  # invisible center == nonexistent (404)
    data = await gs.get_neighborhood(db, node_id, viewer, hops=hops)
    return GraphNeighborhoodOut(**data)


@router.get(
    "/overview",
    response_model=GraphNeighborhoodOut,
    summary="Get graph overview",
    operation_id="getGraphOverview",
)
async def get_overview(
    limit: int = Query(100, ge=1, le=500),
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> GraphNeighborhoodOut:
    data = await gs.get_overview(db, viewer, limit=limit)
    return GraphNeighborhoodOut(**data)
