import uuid

import jwt as pyjwt
from fastapi import Depends, HTTPException, Query, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_token
from app.models.group import GroupMember
from app.models.user import Role

# THE auth-context type (kb-conventions): the canonical Viewer lives in the
# visibility service. deps re-exports it so routers depend on one type only —
# a structural duplicate here breaks mypy at every service call site.
from app.services.visibility import Viewer

_bearer = HTTPBearer(auto_error=False)

__all__ = [
    "Viewer",
    "get_current_viewer",
    "get_scoped_viewer",
    "get_ws_viewer",
    "require_admin",
    "Pagination",
]


async def _viewer_from_token(token: str, db: AsyncSession) -> Viewer:
    """Decode an access token into the canonical Viewer (raises PyJWTError)."""
    claims = decode_token(token, "access")
    uid = uuid.UUID(claims["sub"])
    rows = await db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == uid))
    return Viewer(user_id=uid, role=Role(claims["role"]), group_ids=frozenset(rows))


async def get_current_viewer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Viewer:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        return await _viewer_from_token(creds.credentials, db)
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc


async def get_ws_viewer(websocket: WebSocket, db: AsyncSession) -> Viewer | None:
    """Authenticate a WebSocket handshake; None means "close, don't stream".

    Browsers cannot set an Authorization header on a WebSocket upgrade, so the
    HTTPBearer dependency is unusable here. Two credential carriers instead:
    - `?token=` query param (explicit clients, wscat, tests);
    - the `access_token` httpOnly cookie — the browser talks to the Next.js
      BFF (ADR-008) and cookies flow on the same-origin WS handshake.
    Same verified-JWT trust root as get_current_viewer; like get_scoped_viewer,
    an admin is scoped down to role=user — /api/v1 WS routes are not the
    audited admin console (kb-visibility-filter rule 5).
    """
    token = websocket.query_params.get("token") or websocket.cookies.get("access_token")
    if not token:
        return None
    try:
        viewer = await _viewer_from_token(token, db)
    except pyjwt.PyJWTError:
        return None
    if viewer.role is Role.admin:
        return Viewer(user_id=viewer.user_id, role=Role.user, group_ids=viewer.group_ids)
    return viewer


async def get_scoped_viewer(viewer: Viewer = Depends(get_current_viewer)) -> Viewer:
    """Viewer for routes OUTSIDE /api/v1/admin/*.

    The admin bypass in visible_nodes_clause (kb-visibility-filter rule 5) is
    only allowed on audited admin-console routes. Here an admin's Viewer is
    scoped down to role=user so regular routes never read (or mutate) another
    user's non-public nodes. The role itself still comes from the verified JWT.
    """
    if viewer.role is Role.admin:
        return Viewer(user_id=viewer.user_id, role=Role.user, group_ids=viewer.group_ids)
    return viewer


async def require_admin(viewer: Viewer = Depends(get_current_viewer)) -> Viewer:
    if viewer.role is not Role.admin:
        raise HTTPException(status_code=403, detail="admin required")
    return viewer


class Pagination:
    """Shared `?limit=&offset=` dependency; limit capped at 100 (kb-api-conventions)."""

    def __init__(
        self,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ) -> None:
        self.offset = offset
        self.limit = limit
