from app.models.user import Role
from app.services import auth_service


async def _token(client, email, pw):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_admin_creates_group_and_adds_member(db, client) -> None:
    await auth_service.register(
        db, email="root@x.com", password="rootpw!123", display_name="Root", role=Role.admin
    )
    member = await auth_service.register(
        db, email="m@x.com", password="mpw!12345", display_name="M"
    )
    headers = await _token(client, "root@x.com", "rootpw!123")

    resp = await client.post(
        "/api/v1/admin/groups",
        headers=headers,
        json={"name": "game-team", "description": "Game dev"},
    )
    assert resp.status_code == 201
    gid = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/admin/groups/{gid}/members",
        headers=headers,
        json={"user_id": str(member.id), "role": "member"},
    )
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/admin/groups/{gid}", headers=headers)
    assert str(member.id) in [m["user_id"] for m in resp.json()["members"]]
