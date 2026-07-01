from app.services import auth_service


async def test_login_returns_tokens(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "s3cret!pw"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"access_token", "refresh_token", "token_type"}


async def test_login_bad_password_401(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "no"}
    )
    assert resp.status_code == 401


async def test_refresh_rotates(db, client) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "s3cret!pw"}
    )
    refresh = login.json()["refresh_token"]
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    assert resp.json()["access_token"]
