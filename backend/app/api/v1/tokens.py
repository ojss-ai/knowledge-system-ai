"""Service token API — create/list/revoke API tokens for CLI tools.

Tokens are stored HASHED (argon2, same hasher as passwords); the raw token is
returned exactly once, in the creation response, and never again.

The queries here are sanctioned raw queries (uploads/daily_logs precedent):
`api_tokens` is credential bookkeeping owned by exactly one user, not a
knowledge read path — ownership (owner_id == viewer.user_id) is the whole
visibility rule, answered 404-generic like any invisible read.

[plan-fix] vs the Task 6.2 block:
- `get_scoped_viewer`, not `get_current_viewer`: the admin bypass is only
  reachable under /api/v1/admin/* (Phase 1 standard, kb-visibility rule 5).
- Revoking another user's token answers a generic 404 (invisible ==
  nonexistent, get_run standard) instead of the plan's 403 — a 403 confirms
  the token id exists. `ForbiddenError` import dropped with it.
- `ApiToken.revoked == False` → `.is_(False)` (ruff E712).
- Typed `viewer: Viewer`, return annotations, summary/operation_id added per
  kb-api-conventions.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_scoped_viewer
from app.core.errors import NotFoundError
from app.models.ingest import ApiToken

router = APIRouter(prefix="/tokens", tags=["tokens"])
_hasher = PasswordHasher()


class TokenCreate(BaseModel):
    name: str
    scopes: list[str] = ["read"]


class TokenCreated(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    token: str  # raw token — shown once only


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    created_at: datetime
    revoked: bool

    model_config = {"from_attributes": True}


@router.post(
    "",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a service token (raw token shown once)",
    operation_id="createToken",
)
async def create_token(
    payload: TokenCreate,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> TokenCreated:
    # Format: kb_<token-id-hex>.<secret> — the embedded id makes bearer-auth
    # lookup O(1) by primary key (argon2 hashes are salted and cannot be
    # searched by value); argon2 then verifies the FULL raw token.
    token_id = uuid.uuid4()
    raw = f"kb_{token_id.hex}.{secrets.token_urlsafe(32)}"
    token = ApiToken(
        id=token_id,
        owner_id=viewer.user_id,
        name=payload.name,
        token_hash=_hasher.hash(raw),
        scopes=payload.scopes,
    )
    db.add(token)
    await db.commit()
    return TokenCreated(id=token.id, name=token.name, scopes=token.scopes, token=raw)


@router.get(
    "",
    response_model=list[TokenOut],
    summary="List my active service tokens",
    operation_id="listTokens",
)
async def list_tokens(
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> list[TokenOut]:
    rows = await db.scalars(
        select(ApiToken).where(ApiToken.owner_id == viewer.user_id, ApiToken.revoked.is_(False))
    )
    return [TokenOut.model_validate(row) for row in rows]


@router.delete(
    "/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a service token",
    operation_id="revokeToken",
)
async def revoke_token(
    token_id: uuid.UUID,
    viewer: Viewer = Depends(get_scoped_viewer),
    db: AsyncSession = Depends(get_db),
) -> None:
    token = await db.scalar(select(ApiToken).where(ApiToken.id == token_id))
    if token is None or token.owner_id != viewer.user_id:
        # Generic body: invisible == nonexistent, nothing confirmed either way.
        raise NotFoundError("Token not found")
    token.revoked = True
    await db.commit()
