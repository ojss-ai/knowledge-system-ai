from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserOut
from app.services import auth_service

router = APIRouter(prefix="/users", tags=["admin"])


class UserListOut(BaseModel):
    items: list[UserOut]
    total: int


@router.post("", response_model=UserOut, status_code=201, summary="Create user", operation_id="adminCreateUser")
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await auth_service.register(
        db, email=payload.email, password=payload.password,
        display_name=payload.display_name, role=payload.role,
    )
    return UserOut.model_validate(user)


@router.get("", response_model=UserListOut, summary="List users", operation_id="adminListUsers")
async def list_users(
    limit: int = Query(50, le=100), offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> UserListOut:
    total = await db.scalar(select(func.count()).select_from(User))
    rows = await db.scalars(select(User).order_by(User.created_at).limit(limit).offset(offset))
    return UserListOut(items=[UserOut.model_validate(u) for u in rows], total=total or 0)
