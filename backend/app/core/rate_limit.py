"""
Redis sliding-window rate limiter.
Applied per user_id to expensive endpoints (/ask, /search).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

from app.core.security import _get_redis

_LIMITS: dict[str, tuple[int, int]] = {
    "/api/v1/ask": (20, 60),  # 20 requests per 60 seconds
    "/api/v1/search": (60, 60),  # 60 requests per 60 seconds
}


async def rate_limit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    path = request.url.path
    limit_config = None
    for pattern, config in _LIMITS.items():
        if path.startswith(pattern):
            limit_config = config
            break

    if limit_config is None:
        return await call_next(request)

    max_requests, window_seconds = limit_config

    # Get user identity from JWT (if present)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return await call_next(request)

    user_key = hashlib.sha256(auth.encode()).hexdigest()[:16]
    redis_key = f"rate:{path}:{user_key}"

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
        results = await pipe.execute()
        count: int = results[2]
    except Exception:
        # Redis unavailable — fail open (don't block legitimate requests)
        return await call_next(request)

    if count > max_requests:
        # [plan-fix]: the plan raised HTTPException here, but BaseHTTPMiddleware
        # runs OUTSIDE FastAPI's ExceptionMiddleware, so the exception would
        # never be converted to a 429 response (it surfaces as a server error).
        # Return the JSON response directly instead.
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": f"Rate limit exceeded: {max_requests} requests per {window_seconds}s"
            },
            headers={"Retry-After": str(window_seconds)},
        )

    return await call_next(request)
