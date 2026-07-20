import uuid
from datetime import UTC, datetime

from app.schemas.node import NodeCreate, NodeOut, NodeUpdate


def test_node_create_defaults():
    n = NodeCreate(title="Hello")
    assert n.body == ""
    assert n.visibility.value == "private"


def test_node_out_no_internal_fields():
    data = dict(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        title="T",
        body="B",
        node_type="note",
        visibility="private",
        source=None,
        source_ref=None,
        meta={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    out = NodeOut(**data)
    dumped = out.model_dump()
    assert "password_hash" not in dumped
    assert "body_tsv" not in dumped
    assert "deleted_at" not in dumped


def test_node_update_partial():
    u = NodeUpdate(title="New")
    assert u.body is None
    assert u.title == "New"
