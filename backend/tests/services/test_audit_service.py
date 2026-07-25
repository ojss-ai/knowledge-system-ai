import json

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
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

    rows = await db.scalars(select(AuditLog).where(AuditLog.actor_id == owner.id))
    entries = list(rows)
    assert len(entries) == 1
    assert entries[0].action == "node.create"


async def test_log_meta_over_4kb_truncated_with_marker(db, make_user):
    """3.R.1: oversized meta payloads are capped (~4KB serialized) and replaced
    with an explicit truncation marker — audit rows must stay small even when
    Task 6 admin reads pass cross-user data through here."""
    owner = await make_user(email="audit-cap@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await audit.log(db, viewer=viewer, action="admin.read", meta={"blob": "x" * 10_000})
    await db.flush()

    entry = await db.scalar(select(AuditLog).where(AuditLog.actor_id == owner.id))
    assert entry is not None
    assert entry.meta["truncated"] is True
    assert len(json.dumps(entry.meta).encode()) <= 4096 + 128  # marker overhead only


async def test_log_meta_small_stored_verbatim(db, make_user):
    """3.R.1: payloads under the cap pass through untouched."""
    owner = await make_user(email="audit-small@test.com")
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    await audit.log(db, viewer=viewer, action="admin.read", meta={"k": "v"})
    await db.flush()

    entry = await db.scalar(select(AuditLog).where(AuditLog.actor_id == owner.id))
    assert entry is not None
    assert entry.meta == {"k": "v"}
