from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_current_viewer
from app.core.errors import NotFoundError
from app.models.user import User
from app.schemas.user import UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="Current user profile", operation_id="getMe")
async def me(
    viewer: Viewer = Depends(get_current_viewer), db: AsyncSession = Depends(get_db)
) -> UserOut:
    user = await db.get(User, viewer.user_id)
    if user is None:
        raise NotFoundError("user not found")
    return UserOut.model_validate(user)
