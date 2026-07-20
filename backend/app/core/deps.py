import uuid
from dataclasses import dataclass

import jwt as pyjwt
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_token
from app.models.group import GroupMember
from app.models.user import Role

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Viewer:
    user_id: uuid.UUID
    role: Role
    group_ids: frozenset[uuid.UUID]


async def get_current_viewer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Viewer:
    if creds is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        claims = decode_token(creds.credentials, "access")
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    uid = uuid.UUID(claims["sub"])
    rows = await db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == uid))
    return Viewer(user_id=uid, role=Role(claims["role"]), group_ids=frozenset(rows))


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
