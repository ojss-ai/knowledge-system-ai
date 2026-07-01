from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models.user import Role, User

_hasher = PasswordHasher()


async def register(
    db: AsyncSession, *, email: str, password: str, display_name: str, role: Role = Role.user
) -> User:
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError(f"email already registered: {email}")
    user = User(
        email=email, password_hash=_hasher.hash(password), display_name=display_name, role=role
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if user is None:
        return None
    try:
        _hasher.verify(user.password_hash, password)
    except VerifyMismatchError:
        return None
    return user
