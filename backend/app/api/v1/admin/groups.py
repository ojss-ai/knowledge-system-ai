import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import Viewer, get_current_viewer
from app.core.errors import NotFoundError
from app.models.group import Group, GroupMember
from app.schemas.group import GroupCreate, GroupDetailOut, GroupMemberIn, GroupMemberOut, GroupOut

router = APIRouter(prefix="/groups", tags=["admin"])


@router.post(
    "",
    response_model=GroupOut,
    status_code=201,
    summary="Create group",
    operation_id="adminCreateGroup",
)
async def create_group(
    payload: GroupCreate,
    viewer: Viewer = Depends(get_current_viewer),
    db: AsyncSession = Depends(get_db),
) -> GroupOut:
    group = Group(name=payload.name, description=payload.description, created_by=viewer.user_id)
    db.add(group)
    await db.flush()
    return GroupOut.model_validate(group)


@router.get(
    "/{group_id}",
    response_model=GroupDetailOut,
    summary="Group detail",
    operation_id="adminGetGroup",
)
async def get_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> GroupDetailOut:
    group = await db.get(Group, group_id)
    if group is None:
        raise NotFoundError("group not found")
    members = (await db.scalars(select(GroupMember).where(GroupMember.group_id == group_id))).all()
    return GroupDetailOut(
        id=group.id,
        name=group.name,
        description=group.description,
        members=[GroupMemberOut.model_validate(m) for m in members],
    )


@router.post(
    "/{group_id}/members", status_code=204, summary="Add member", operation_id="adminAddGroupMember"
)
async def add_member(
    group_id: uuid.UUID, payload: GroupMemberIn, db: AsyncSession = Depends(get_db)
) -> None:
    if await db.get(Group, group_id) is None:
        raise NotFoundError("group not found")
    await db.merge(GroupMember(group_id=group_id, user_id=payload.user_id, role=payload.role))
    await db.flush()
