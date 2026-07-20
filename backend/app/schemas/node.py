import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import Visibility


class NodeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    body: str = ""
    node_type: str = "note"
    visibility: Visibility = Visibility.private
    source: str | None = None
    source_ref: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class NodeUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=512)
    body: str | None = None
    visibility: Visibility | None = None
    meta: dict[str, Any] | None = None


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    body: str
    node_type: str
    visibility: Visibility
    source: str | None
    source_ref: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class NodeListOut(BaseModel):
    items: list[NodeOut]
    total: int
    offset: int
    limit: int


class NodeShareCreate(BaseModel):
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    can_edit: bool = False


class GraphNeighborhoodOut(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
