from app.models.user import Role, User


async def test_user_roundtrip(db) -> None:
    user = User(email="a@example.com", password_hash="x", display_name="A", role=Role.user)
    db.add(user)
    await db.flush()
    assert user.id is not None
    assert user.role is Role.user
    assert user.is_active is True
