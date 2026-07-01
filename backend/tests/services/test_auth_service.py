import pytest

from app.core.errors import ConflictError
from app.services import auth_service


async def test_register_and_authenticate(db) -> None:
    user = await auth_service.register(
        db, email="a@example.com", password="s3cret!pw", display_name="A"
    )
    assert user.password_hash != "s3cret!pw"  # hashed, never plaintext

    ok = await auth_service.authenticate(db, "a@example.com", "s3cret!pw")
    assert ok is not None and ok.id == user.id

    bad = await auth_service.authenticate(db, "a@example.com", "wrong")
    assert bad is None


async def test_register_duplicate_email_conflicts(db) -> None:
    await auth_service.register(db, email="a@example.com", password="s3cret!pw", display_name="A")
    with pytest.raises(ConflictError):
        await auth_service.register(db, email="a@example.com", password="other", display_name="B")
