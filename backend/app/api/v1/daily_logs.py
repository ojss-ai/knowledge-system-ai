"""Daily logs router — daily logs are just KnowledgeNodes with node_type=daily_log
(ADR-012), keyed per user by source="daily_log" + source_ref=ISO date.

POST is an upsert-by-date: the existence probe here is the one sanctioned raw
query outside node_service (exempted by name in the Phase 1 visibility audit);
it still composes visible_nodes_clause (kb-visibility-filter rule 1).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_scoped_viewer
from app.core.errors import ConflictError, NotFoundError
from app.models.knowledge import KnowledgeNode, NodeType
from app.models.user import Visibility
from app.schemas.node import NodeOut
from app.services import node_service as ns
from app.services.visibility import Viewer, visible_nodes_clause

router = APIRouter(prefix="/daily-logs", tags=["daily_logs"])


class DailyLogCreate(BaseModel):
    date: date
    body: str = ""


async def _existing_log(db: AsyncSession, viewer: Viewer, date_str: str) -> KnowledgeNode | None:
    """The sanctioned existence probe (module docstring): this user's log for a date."""
    row: KnowledgeNode | None = await db.scalar(
        select(KnowledgeNode).where(
            visible_nodes_clause(viewer),
            KnowledgeNode.owner_id == viewer.user_id,
            KnowledgeNode.node_type == NodeType.daily_log.value,
            KnowledgeNode.source == "daily_log",
            KnowledgeNode.source_ref == date_str,
        )
    )
    return row


@router.post(
    "",
    response_model=NodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create or update the daily log for a date",
    operation_id="upsertDailyLog",
)
async def upsert_daily_log(
    payload: DailyLogCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    date_str = payload.date.isoformat()
    # Check if log already exists for this user+date
    existing = await _existing_log(db, viewer, date_str)
    if existing:
        node = await ns.update_node(db, existing.id, viewer, body=payload.body)
    else:
        try:
            # SAVEPOINT: if the INSERT hits uq_node_owner_source_ref, only this
            # block rolls back and the session stays usable for the re-fetch.
            async with db.begin_nested():
                node = await ns.create_node(
                    db,
                    viewer=viewer,
                    title=f"Daily Log — {date_str}",
                    body=payload.body,
                    node_type=NodeType.daily_log.value,
                    visibility=Visibility.private,
                    source="daily_log",
                    source_ref=date_str,
                )
        except ConflictError:
            # SELECT-then-INSERT race: a concurrent POST created this user's log
            # between our probe and the flush. Upsert semantics — converge on the
            # existing row and update it, don't surface a 409.
            existing = await _existing_log(db, viewer, date_str)
            if existing is None:  # key held by a row the probe can't see (soft-deleted)
                raise
            node = await ns.update_node(db, existing.id, viewer, body=payload.body)
    await db.commit()
    await ns.run_pending_graph_ops(db)  # Neo4j strictly after PG commit (ADR-011)
    return NodeOut.model_validate(node)


@router.get(
    "/{log_date}",
    response_model=NodeOut,
    summary="Get the daily log for a date",
    operation_id="getDailyLog",
)
async def get_daily_log(
    log_date: date,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    date_str = log_date.isoformat()
    node = await db.scalar(
        select(KnowledgeNode).where(
            visible_nodes_clause(viewer),
            KnowledgeNode.node_type == NodeType.daily_log.value,
            KnowledgeNode.source_ref == date_str,
        )
    )
    if node is None:
        # Generic body (get_node standard): invisible == nonexistent, and the
        # message must not confirm anything about what exists.
        raise NotFoundError("Daily log not found")
    return NodeOut.model_validate(node)


@router.get(
    "",
    response_model=list[NodeOut],
    summary="List recent daily logs",
    operation_id="listDailyLogs",
)
async def list_daily_logs(
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> list[NodeOut]:
    rows = await db.scalars(
        select(KnowledgeNode)
        .where(
            visible_nodes_clause(viewer),
            KnowledgeNode.node_type == NodeType.daily_log.value,
        )
        .order_by(KnowledgeNode.source_ref.desc())
        .limit(90)  # last 90 days
    )
    return [NodeOut.model_validate(n) for n in rows]
