"""Nodes CRUD router — thin translation layer over node_service (ADR-005).

Mutation handlers commit the session themselves, then run the graph ops the
service queued: Neo4j is written strictly AFTER the PG commit (ADR-011).
Viewer comes from get_scoped_viewer — the admin visibility bypass is only
reachable under /api/v1/admin/* (kb-visibility-filter rule 5).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Pagination, Viewer, get_scoped_viewer
from app.schemas.node import NodeCreate, NodeListOut, NodeOut, NodeShareCreate, NodeUpdate
from app.services import node_service as ns

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post(
    "",
    response_model=NodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create node",
    operation_id="createNode",
)
async def create_node(
    payload: NodeCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    node = await ns.create_node(
        db,
        viewer=viewer,
        title=payload.title,
        body=payload.body,
        node_type=payload.node_type,
        visibility=payload.visibility,
        source=payload.source,
        source_ref=payload.source_ref,
        meta=payload.meta,
    )
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)
    return NodeOut.model_validate(node)


@router.get("", response_model=NodeListOut, summary="List nodes", operation_id="listNodes")
async def list_nodes(
    pagination: Pagination = Depends(),
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeListOut:
    items, total = await ns.list_nodes(db, viewer, offset=pagination.offset, limit=pagination.limit)
    return NodeListOut(
        items=[NodeOut.model_validate(n) for n in items],
        total=total,
        offset=pagination.offset,
        limit=pagination.limit,
    )


@router.get("/{node_id}", response_model=NodeOut, summary="Get node", operation_id="getNode")
async def get_node(
    node_id: uuid.UUID,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    node = await ns.get_node(db, node_id, viewer)
    return NodeOut.model_validate(node)


@router.patch(
    "/{node_id}", response_model=NodeOut, summary="Update node", operation_id="updateNode"
)
async def update_node(
    node_id: uuid.UUID,
    payload: NodeUpdate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    node = await ns.update_node(
        db,
        node_id,
        viewer,
        title=payload.title,
        body=payload.body,
        visibility=payload.visibility,
        meta=payload.meta,
    )
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)
    return NodeOut.model_validate(node)


@router.delete(
    "/{node_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete node",
    operation_id="deleteNode",
)
async def delete_node(
    node_id: uuid.UUID,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> None:
    await ns.delete_node(db, node_id, viewer)
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)


@router.post(
    "/{node_id}/shares",
    response_model=NodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Share node with a user or group",
    operation_id="shareNode",
)
async def share_node(
    node_id: uuid.UUID,
    payload: NodeShareCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    node = await ns.share_node(
        db,
        node_id,
        viewer,
        user_id=payload.user_id,
        group_id=payload.group_id,
        can_edit=payload.can_edit,
    )
    await db.commit()
    return NodeOut.model_validate(node)
