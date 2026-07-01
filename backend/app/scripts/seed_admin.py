"""Idempotent admin seeder: python -m app.scripts.seed_admin email password"""

import asyncio
import sys

from app.core.db import SessionLocal
from app.core.errors import ConflictError
from app.models.user import Role
from app.services import auth_service


async def main(email: str, password: str) -> None:
    async with SessionLocal() as db:
        try:
            await auth_service.register(
                db, email=email, password=password, display_name="Admin", role=Role.admin
            )
            await db.commit()
            print(f"admin created: {email}")
        except ConflictError:
            print(f"admin already exists: {email}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
