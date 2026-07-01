import uuid

from pydantic import BaseModel, ConfigDict

from app.models.group import GroupRole


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupMemberIn(BaseModel):
    user_id: uuid.UUID
    role: GroupRole = GroupRole.member


class GroupMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: uuid.UUID
    role: GroupRole


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str


class GroupDetailOut(GroupOut):
    members: list[GroupMemberOut]
