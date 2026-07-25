"""Admin dashboard stats.

Admin-console read (kb-visibility-filter rule 5): the node count intentionally
skips visible_nodes_clause — this is the audited admin bypass, allowed only
under /api/v1/admin/*. Every call writes an audit entry via audit_service.
require_admin is applied at the package router include (admin/__init__.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_current_viewer
from app.models.audit import AuditLog
from app.models.chunk import NodeChunk
from app.models.knowledge import KnowledgeNode
from app.models.user import User
from app.services import audit_service

router = APIRouter(tags=["admin"])


class StatsOut(BaseModel):
    total_users: int
    active_users: int
    total_nodes: int
    total_chunks: int
    total_audit_events: int


@router.get(
    "/stats",
    response_model=StatsOut,
    summary="Admin dashboard stats",
    operation_id="adminGetStats",
)
async def get_stats(
    # Full-role viewer (NOT get_scoped_viewer): admin routes are the audited
    # exception (kb-visibility-filter rule 5); needed as the audit actor.
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
) -> StatsOut:
    total_users = await db.scalar(select(func.count()).select_from(User)) or 0
    active_users = (
        await db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True))) or 0
    )
    total_nodes = (
        await db.scalar(
            select(func.count())
            .select_from(KnowledgeNode)
            .where(KnowledgeNode.deleted_at.is_(None))
        )
        or 0
    )
    total_chunks = await db.scalar(select(func.count()).select_from(NodeChunk)) or 0
    total_audit = await db.scalar(select(func.count()).select_from(AuditLog)) or 0
    stats = StatsOut(
        total_users=total_users,
        active_users=active_users,
        total_nodes=total_nodes,
        total_chunks=total_chunks,
        total_audit_events=total_audit,
    )
    # Carry-over (phase-1 Task 8): counts aggregate other users' non-public
    # nodes — the admin read itself is audit-logged. Meta = counts only.
    await audit_service.log(
        db,
        viewer=viewer,
        action="admin.stats.read",
        resource_type="stats",
        meta=stats.model_dump(),
    )
    return stats
