"""
Redis sliding-window rate limiter.
Applied per user_id to expensive endpoints (/ask, /search).
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Awaitable, Callable

import jwt as pyjwt
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.security import ALGO, _get_redis

_LIMITS: dict[str, tuple[int, int]] = {
    "/api/v1/ask": (20, 60),  # 20 requests per 60 seconds
    "/api/v1/search": (60, 60),  # 60 requests per 60 seconds
}


def _limit_for(path: str) -> tuple[int, int] | None:
    """Segment-bound match [5.R.3]: '/api/v1/ask' and '/api/v1/ask/...' are
    limited; an unrelated sibling like '/api/v1/askew' is not."""
    for prefix, config in _LIMITS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return config
    return None


def _bucket_key(auth_header: str) -> str:
    """Rate-limit identity [5.R.1]: the token's `sub` claim (user id), so every
    token a user holds draws from ONE bucket — keying on the raw header would
    hand out a fresh budget per login. Invalid/undecodable token → hash of the
    header (the request is still limited; auth rejects it downstream anyway).
    """
    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = pyjwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
        sub = claims.get("sub")
        if sub:
            return f"sub:{sub}"
    except pyjwt.PyJWTError:
        pass
    return "tok:" + hashlib.sha256(auth_header.encode()).hexdigest()[:16]


async def rate_limit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    limit_config = _limit_for(path)
    if limit_config is None:
        return await call_next(request)

    max_requests, window_seconds = limit_config

    # Get user identity from JWT (if present)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await call_next(request)

    redis_key = f"rate:{path}:{_bucket_key(auth)}"

    try:
        r = await _get_redis()
        now = time.time()
        window_start = now - window_seconds

        # Sliding window using sorted set
        pipe = r.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zadd(redis_key, {str(now): now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        pipe.zrange(redis_key, 0, 0, withscores=True)  # oldest entry, for Retry-After
        results = await pipe.execute()
        count: int = results[2]
        oldest: list[tuple[str, float]] = results[4]
    except Exception:
        # Redis unavailable — fail open (don't block legitimate requests)
        return await call_next(request)

    if count > max_requests:
        # [plan-fix]: the plan raised HTTPException here, but BaseHTTPMiddleware
        # runs OUTSIDE FastAPI's ExceptionMiddleware, so the exception would
        # never be converted to a 429 response (it surfaces as a server error).
        # Return the JSON response directly instead.
        # [5.R.2] Retry-After = when the oldest window entry ages out (a slot
        # frees then), not a hardcoded window; bounded to [1, window].
        retry_after = window_seconds
        if oldest:
            frees_in = math.ceil(oldest[0][1] + window_seconds - now)
            retry_after = min(max(frees_in, 1), window_seconds)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": f"Rate limit exceeded: {max_requests} requests per {window_seconds}s"
            },
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)
