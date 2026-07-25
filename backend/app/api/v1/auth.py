import time
import uuid

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import (
    claim_jti_once,
    decode_token,
    make_access_token,
    make_refresh_token,
)
from app.schemas.auth import LoginIn, RefreshIn, TokensOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokensOut, summary="Log in", operation_id="login")
async def login(payload: LoginIn, db: AsyncSession = Depends(get_db)) -> TokensOut:
    user = await auth_service.authenticate(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(
            status_code=401, detail="invalid credentials"
        )  # auth boundary, not domain
    return TokensOut(
        access_token=make_access_token(user.id, user.role.value),
        refresh_token=make_refresh_token(user.id, user.role.value),
    )


@router.post(
    "/refresh", response_model=TokensOut, summary="Rotate tokens", operation_id="refreshTokens"
)
async def refresh(payload: RefreshIn, db: AsyncSession = Depends(get_db)) -> TokensOut:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid refresh token") from exc

    # ADR-008: rotation + revocation — a refresh token is single-use.
    # [4.R.1] Single atomic claim (SET NX EX): the old check-then-set pair let two
    # concurrent refreshes with the same token both succeed. Exactly one caller
    # may claim the JTI; everyone else is a reuse.
    jti = claims.get("jti")
    if jti:
        remaining = max(int(claims["exp"] - time.time()), 1)
        try:
            claimed = await claim_jti_once(jti, remaining)
        except (RedisError, OSError) as exc:
            # [4.R.2] Fail CLOSED: without Redis, single-use cannot be enforced,
            # so rotating tokens blind would reopen the replay hole. Generic
            # detail — no internals leak across the auth boundary.
            raise HTTPException(status_code=503, detail="service temporarily unavailable") from exc
        if not claimed:
            raise HTTPException(status_code=401, detail="refresh token reused")

    user = await auth_service.get_active_user(db, uuid.UUID(claims["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")  # auth boundary

    return TokensOut(
        access_token=make_access_token(user.id, user.role.value),
        refresh_token=make_refresh_token(user.id, user.role.value),
    )
