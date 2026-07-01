from app.models.user import Role
from app.services import auth_service


async def _token(client, email, pw):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_admin_creates_and_lists_users(db, client) -> None:
    await auth_service.register(
        db, email="root@x.com", password="rootpw!123", display_name="Root", role=Role.admin
    )
    headers = await _token(client, "root@x.com", "rootpw!123")

    resp = await client.post(
        "/api/v1/admin/users",
        headers=headers,
        json={"email": "b@x.com", "password": "bpw!12345", "display_name": "B"},
    )
    assert resp.status_code == 201

    resp = await client.get("/api/v1/admin/users?limit=50&offset=0", headers=headers)
    assert resp.status_code == 200
    emails = [u["email"] for u in resp.json()["items"]]
    assert "b@x.com" in emails


async def test_non_admin_gets_403(db, client) -> None:
    await auth_service.register(db, email="u@x.com", password="userpw!123", display_name="U")
    headers = await _token(client, "u@x.com", "userpw!123")
    resp = await client.get("/api/v1/admin/users", headers=headers)
    assert resp.status_code == 403
