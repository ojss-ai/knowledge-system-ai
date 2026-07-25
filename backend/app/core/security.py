import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import redis.asyncio as aioredis

from app.core.config import settings

ALGO = "HS256"

_redis: aioredis.Redis | None = None
_redis_loop: asyncio.AbstractEventLoop | None = None


async def _get_redis() -> aioredis.Redis:
    # [plan-fix]: plan cached one global client forever; an async Redis client is
    # bound to the event loop it was created on, so a stale client raises
    # "Event loop is closed" (one loop per test; also any loop restart).
    # Memoize per running loop — identical behavior under uvicorn's single loop.
    global _redis, _redis_loop
    loop = asyncio.get_running_loop()
    if _redis is None or _redis_loop is not loop:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        _redis_loop = loop
    return _redis


async def revoke_jti(jti: str, ttl_seconds: int) -> None:
    """Add a JTI to the revocation set in Redis (ADR-008)."""
    r = await _get_redis()
    await r.set(f"revoked_jti:{jti}", "1", ex=ttl_seconds)  # [plan-fix]: setex deprecated


async def is_jti_revoked(jti: str) -> bool:
    r = await _get_redis()
    return bool(await r.exists(f"revoked_jti:{jti}") == 1)


def _make(sub: str, role: str, ttl: int, kind: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": sub,
            "role": role,
            "kind": kind,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=ttl),
        },
        settings.jwt_secret,
        algorithm=ALGO,
    )


def make_access_token(user_id: uuid.UUID, role: str) -> str:
    return _make(str(user_id), role, settings.jwt_access_ttl_seconds, "access")


def make_refresh_token(user_id: uuid.UUID, role: str) -> str:
    return _make(str(user_id), role, settings.jwt_refresh_ttl_seconds, "refresh")


def decode_token(token: str, expected_kind: str) -> dict[str, Any]:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
    if payload.get("kind") != expected_kind:
        raise jwt.InvalidTokenError("wrong token kind")
    return payload
