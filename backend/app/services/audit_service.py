from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.services.visibility import Viewer

_META_MAX_BYTES = 4096  # [3.R.1] cap serialized meta; audit rows must stay small


def _cap_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Bound `meta` to ~4KB serialized [3.R.1].

    Oversized payloads are replaced by a marker dict with a UTF-8-safe preview —
    the audit trail records THAT something happened, it is not a payload store.
    """
    if not meta:
        return {}
    serialized = json.dumps(meta, default=str)
    if len(serialized.encode("utf-8")) <= _META_MAX_BYTES:
        return meta
    preview = serialized.encode("utf-8")[: _META_MAX_BYTES - 64].decode("utf-8", errors="ignore")
    return {"truncated": True, "preview": preview}


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
    """Append an audit entry (committed with the caller's transaction).

    [3.R.1] `meta` is stored verbatim in JSONB and surfaced on admin dashboards:
    do NOT put secrets or PII in it (passwords, tokens, node bodies, other
    users' personal data) — Task 6 admin reads pass cross-user data through
    here, so keep meta to ids/titles/counts. Payloads whose JSON form exceeds
    ~4KB are truncated to a marker dict.
    """
    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=viewer.user_id if viewer else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        meta=_cap_meta(meta),
    )
    db.add(entry)
    # Non-blocking: do not flush here — caller's transaction commits it
