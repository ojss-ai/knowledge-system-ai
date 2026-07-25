"""Task 4 — JWT refresh revocation (ADR-008: revocation list in Redis).

[plan-fix]: the plan's test used POST /api/v1/auth/register and form-data login;
the real API has JSON login only and no register endpoint (see conftest
[plan-fix, Task 8.5]). Register via auth_service instead. `pytestmark asyncio`
dropped — asyncio_mode is "auto".
"""

from app.services import auth_service


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
