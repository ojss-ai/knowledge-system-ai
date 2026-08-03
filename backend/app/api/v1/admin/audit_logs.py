"""Admin audit-log browser.

Audit entries record other users' actions (non-public data), so reading them
is itself an audited admin read (kb-visibility-filter rule 5 carry-over).
require_admin is applied at the package router include (admin/__init__.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Pagination, Viewer, get_current_viewer
from app.models.audit import AuditLog
from app.services import audit_service

router = APIRouter(tags=["admin"])


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    created_at: datetime
    meta: dict[str, Any]

    model_config = {"from_attributes": True}


class AuditLogsListOut(BaseModel):
    items: list[AuditLogOut]
    total: int


@router.get(
    "/audit-logs",
    response_model=AuditLogsListOut,
    summary="List audit log entries",
    operation_id="adminListAuditLogs",
)
async def list_audit_logs(
    action: str | None = Query(None, max_length=128),
    page: Pagination = Depends(),
    # Full-role viewer (NOT get_scoped_viewer) — audited admin exception.
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
) -> AuditLogsListOut:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    count_stmt = select(func.count()).select_from(AuditLog)
    if action:
        # [plan-fix] plan's total ignored the action filter — total must count
        # the filtered set or pagination over a filtered list is wrong.
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    total = await db.scalar(count_stmt) or 0
    rows = await db.scalars(stmt.offset(page.offset).limit(page.limit))
    items = [AuditLogOut.model_validate(r) for r in rows]
    # Carry-over (phase-1 Task 8): meta stays small — query params + counts.
    await audit_service.log(
        db,
        viewer=viewer,
        action="admin.audit_logs.read",
        resource_type="audit_log",
        meta={
            "action_filter": action,
            "offset": page.offset,
            "limit": page.limit,
            "returned": len(items),
        },
    )
    return AuditLogsListOut(items=items, total=total)
