from app.services import auth_service


async def _login(client, email, password):
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def test_me_returns_profile(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    token = await _login(client, "a@example.com", "s3cret!pw")
    resp = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "a@example.com"
    assert "password_hash" not in body


async def test_me_unauthenticated_401(client) -> None:
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
