# backend/tests/api/test_admin_api.py
# [plan-fix]: the plan's tests logged in as a seeded admin (admin@kb.local /
# admin1234) via form data; this codebase has no seeded admin and JSON-only
# login — admins are registered through auth_service like every other API test.
# (.local is a reserved TLD that pydantic EmailStr rejects — admin@kb.example.)
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.user import Role
from app.services import auth_service


async def _admin_headers(db, client) -> tuple[dict[str, str], object]:
    admin = await auth_service.register(
        db, email="admin@kb.example", password="admin1234", display_name="Admin", role=Role.admin
    )
    r = await client.post(
        "/api/v1/auth/login", json={"email": "admin@kb.example", "password": "admin1234"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, admin


async def test_admin_stats_requires_auth(client) -> None:
    r = await client.get("/api/v1/admin/stats")
    assert r.status_code == 401


async def test_admin_stats_requires_admin(client, auth_headers) -> None:
    r = await client.get("/api/v1/admin/stats", headers=auth_headers)
    assert r.status_code == 403


async def test_admin_stats_for_admin(db, client) -> None:
    headers, _ = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/stats", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "total_users" in data
    assert "active_users" in data
    assert "total_nodes" in data
    assert "total_chunks" in data
    assert "total_audit_events" in data
    assert data["total_users"] >= 1


async def test_admin_stats_excludes_soft_deleted_nodes(db, client, make_user, make_node) -> None:
    headers, _ = await _admin_headers(db, client)
    owner = await make_user(email="counted@test.com")
    await make_node(owner, title="counted")
    deleted = await make_node(owner, title="soft-deleted")
    from datetime import UTC, datetime

    deleted.deleted_at = datetime.now(UTC)
    await db.flush()

    r = await client.get("/api/v1/admin/stats", headers=headers)
    assert r.status_code == 200
    assert r.json()["total_nodes"] == 1


async def test_admin_audit_log(db, client) -> None:
    headers, _ = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/audit-logs", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body


async def test_admin_audit_logs_requires_admin(client, auth_headers) -> None:
    r = await client.get("/api/v1/admin/audit-logs", headers=auth_headers)
    assert r.status_code == 403


async def test_admin_audit_logs_filter_and_pagination(db, client, make_user) -> None:
    from app.services import audit_service
    from app.services.visibility import Viewer

    headers, _ = await _admin_headers(db, client)
    actor = await make_user(email="actor@test.com")
    viewer = Viewer(user_id=actor.id, role=Role.user, group_ids=frozenset())
    await audit_service.log(db, viewer=viewer, action="node.create")
    await audit_service.log(db, viewer=viewer, action="node.delete")
    await db.flush()

    r = await client.get("/api/v1/admin/audit-logs?action=node.create", headers=headers)
    assert r.status_code == 200
    assert all(item["action"] == "node.create" for item in r.json()["items"])

    r = await client.get("/api/v1/admin/audit-logs?limit=101", headers=headers)
    assert r.status_code == 422  # shared Pagination dep caps limit at 100


# --- Carry-over (phase-1 Task 8 via Task 3): admin reads of cross-user,
# non-public data must themselves be audit-logged (kb-visibility-filter rule 5).


async def test_admin_stats_read_is_audited(db, client) -> None:
    headers, admin = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/stats", headers=headers)
    assert r.status_code == 200

    rows = await db.scalars(
        select(AuditLog).where(AuditLog.actor_id == admin.id, AuditLog.action == "admin.stats.read")
    )
    entries = list(rows)
    assert len(entries) == 1
    assert entries[0].resource_type == "stats"


async def test_admin_audit_logs_read_is_audited(db, client) -> None:
    headers, admin = await _admin_headers(db, client)
    r = await client.get("/api/v1/admin/audit-logs?action=node.create", headers=headers)
    assert r.status_code == 200

    rows = await db.scalars(
        select(AuditLog).where(
            AuditLog.actor_id == admin.id, AuditLog.action == "admin.audit_logs.read"
        )
    )
    entries = list(rows)
    assert len(entries) == 1
    # meta stays small (ids/params/counts only — audit_service caps at 4KB)
    assert entries[0].meta.get("action_filter") == "node.create"
