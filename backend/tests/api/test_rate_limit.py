import time

from httpx import AsyncClient

from app.core.security import _get_redis
from app.services import auth_service

# no pytestmark: asyncio_mode is "auto", and the 5.R.3 test is sync


async def test_rate_limit_on_ask_endpoint(client: AsyncClient, auth_headers):
    """The /ask endpoint should rate-limit after N requests per window."""
    # Send many requests rapidly
    responses = []
    for _ in range(30):
        r = await client.post("/api/v1/ask", json={"query": "test"}, headers=auth_headers)
        responses.append(r.status_code)

    # At least one should be 429
    assert 429 in responses, "Rate limiting must return 429 after burst"


async def test_rate_limit_bucket_shared_across_tokens_of_same_user(db, client):
    """5.R.1: the bucket is keyed on the token's `sub` (user id), not on the raw
    Authorization header — two tokens for the same user share one budget."""
    await auth_service.register(
        db, email="ratelimit@test.com", password="pass1234", display_name="RL"
    )
    creds = {"email": "ratelimit@test.com", "password": "pass1234"}
    t1 = (await client.post("/api/v1/auth/login", json=creds)).json()["access_token"]
    t2 = (await client.post("/api/v1/auth/login", json=creds)).json()["access_token"]
    assert t1 != t2  # distinct tokens (fresh jti), same user

    statuses = []
    for i in range(30):  # 15 per token; only a shared per-user bucket trips 20/60s
        headers = {"Authorization": f"Bearer {t1 if i % 2 else t2}"}
        r = await client.post("/api/v1/ask", json={"query": "x"}, headers=headers)
        statuses.append(r.status_code)
    assert 429 in statuses, "same-user tokens must share one rate-limit bucket"


async def test_retry_after_computed_from_sliding_window(db, client):
    """5.R.2: Retry-After reflects when the oldest window entry ages out, not a
    hardcoded 60. Seed a bucket whose entries are 50s old -> ~10s remain."""
    user = await auth_service.register(
        db, email="retryafter@test.com", password="pass1234", display_name="RA"
    )
    token = (
        await client.post(
            "/api/v1/auth/login",
            json={"email": "retryafter@test.com", "password": "pass1234"},
        )
    ).json()["access_token"]

    r = await _get_redis()
    key = f"rate:/api/v1/ask:sub:{user.id}"
    now = time.time()
    await r.zadd(key, {f"seed{i}": now - 50 for i in range(20)})  # bucket full, 10s left
    await r.expire(key, 61)

    resp = await client.post(
        "/api/v1/ask", json={"query": "x"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 429
    retry_after = int(resp.headers["Retry-After"])
    assert 1 <= retry_after <= 12, f"expected ~10s from window, got {retry_after}"


def test_limit_match_is_segment_bound():
    """5.R.3: path matching is per segment — no accidental prefix captures."""
    from app.core.rate_limit import _limit_for  # in-body: RED = ImportError here only

    assert _limit_for("/api/v1/ask") is not None
    assert _limit_for("/api/v1/ask/stream") is not None
    assert _limit_for("/api/v1/askew") is None
    assert _limit_for("/api/v1/search") is not None
    assert _limit_for("/api/v1/searchable") is None
