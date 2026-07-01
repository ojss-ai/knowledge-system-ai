import uuid
from dataclasses import dataclass

import jwt as pyjwt
from fastapi import Depends, HTTPException
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
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    uid = uuid.UUID(claims["sub"])
    rows = await db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == uid))
    return Viewer(user_id=uid, role=Role(claims["role"]), group_ids=frozenset(rows))


async def require_admin(viewer: Viewer = Depends(get_current_viewer)) -> Viewer:
    if viewer.role is not Role.admin:
        raise HTTPException(status_code=403, detail="admin required")
    return viewer
