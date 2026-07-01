import uuid

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_token, make_access_token, make_refresh_token
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
async def refresh(payload: RefreshIn) -> TokensOut:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid refresh token") from exc
    uid, role = uuid.UUID(claims["sub"]), claims["role"]
    return TokensOut(
        access_token=make_access_token(uid, role), refresh_token=make_refresh_token(uid, role)
    )
