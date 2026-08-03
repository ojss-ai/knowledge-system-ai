"""Task 4 — JWT refresh revocation (ADR-008: revocation list in Redis).

[plan-fix]: the plan's test used POST /api/v1/auth/register and form-data login;
the real API has JSON login only and no register endpoint (see conftest
[plan-fix, Task 8.5]). Register via auth_service instead. `pytestmark asyncio`
dropped — asyncio_mode is "auto".
"""

import asyncio
from typing import Any

import jwt as pyjwt
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import security
from app.core.config import settings
from app.services import auth_service


async def _login(db, client, email: str) -> str:
    """Register + login, return the refresh token."""
    await auth_service.register(db, email=email, password="pass1234", display_name="R")
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "pass1234"})
    return str(r.json()["refresh_token"])


class _BarrierRedis:
    """Proxy that holds `exists` until both racers have checked (4.R.1).

    Makes the check-then-set race deterministic: both concurrent refreshes read
    "not revoked" before either writes. An atomic SET NX claim never calls
    `exists`, so the barrier is simply bypassed by correct code.
    """

    def __init__(self, real: Any, barrier: asyncio.Barrier) -> None:
        self._real = real
        self._barrier = barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def exists(self, *args: Any, **kwargs: Any) -> Any:
        await self._barrier.wait()
        return await self._real.exists(*args, **kwargs)


async def test_concurrent_refresh_same_token_single_winner(db, client, monkeypatch) -> None:
    """4.R.1 (CRITICAL): two refreshes racing with the SAME token — exactly one wins."""
    refresh_token = await _login(db, client, "race@test.com")

    barrier = asyncio.Barrier(2)
    real_get_redis = security._get_redis

    async def _barrier_redis() -> Any:
        return _BarrierRedis(await real_get_redis(), barrier)

    monkeypatch.setattr(security, "_get_redis", _barrier_redis)

    r1, r2 = await asyncio.gather(
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 401]


async def test_refresh_preclaimed_jti_rejected(db, client) -> None:
    """4.R.1 NX semantics: a JTI already claimed in Redis (as a concurrent winner
    leaves it) must be rejected as reuse — SET NX fails on an existing key."""
    refresh_token = await _login(db, client, "preclaim@test.com")
    claims = pyjwt.decode(refresh_token, settings.jwt_secret, algorithms=[security.ALGO])

    r = await security._get_redis()
    await r.set(f"revoked_jti:{claims['jti']}", "1", nx=True, ex=60)

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "refresh token reused"


async def test_refresh_redis_down_fails_closed_503(db, client, monkeypatch) -> None:
    """4.R.2: Redis unreachable — refresh must fail CLOSED with a generic 503
    (single-use cannot be enforced, so tokens must not be rotated blind)."""
    refresh_token = await _login(db, client, "redisdown@test.com")

    class _DownRedis:
        def __getattr__(self, name: str) -> Any:
            async def _fail(*args: Any, **kwargs: Any) -> Any:
                raise RedisConnectionError("redis down")

            return _fail

    async def _down() -> Any:
        return _DownRedis()

    monkeypatch.setattr(security, "_get_redis", _down)

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "service temporarily unavailable"


async def test_refresh_token_used_twice_rejected(db, client) -> None:
    """Using a refresh token a second time must return 401 (rotation + revocation)."""
    await auth_service.register(db, email="revoke@test.com", password="pass1234", display_name="R")
    r = await client.post(
        "/api/v1/auth/login", json={"email": "revoke@test.com", "password": "pass1234"}
    )
    refresh_token = r.json()["refresh_token"]

    # First refresh — should succeed
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r1.status_code == 200

    # Second use of same refresh token — must be rejected
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert r2.status_code == 401
