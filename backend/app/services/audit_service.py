from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.services.visibility import Viewer


async def log(
    db: AsyncSession,
    *,
    viewer: Viewer | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=viewer.user_id if viewer else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        meta=meta or {},
    )
    db.add(entry)
    # Non-blocking: do not flush here — caller's transaction commits it
