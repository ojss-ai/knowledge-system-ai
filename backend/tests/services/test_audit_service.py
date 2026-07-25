import pytest

from app.models.user import Role
from app.services import audit_service as audit
from app.services.visibility import Viewer

pytestmark = pytest.mark.asyncio


async def test_log_action(db, make_user):
    owner = await make_user(email="audit1@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await audit.log(
        db,
        viewer=viewer,
        action="node.create",
        resource_type="node",
        resource_id=str(owner.id),
        meta={"title": "Test"},
    )
    await db.flush()
    from sqlalchemy import select

    from app.models.audit import AuditLog

    rows = await db.scalars(select(AuditLog).where(AuditLog.actor_id == owner.id))
    entries = list(rows)
    assert len(entries) == 1
    assert entries[0].action == "node.create"
