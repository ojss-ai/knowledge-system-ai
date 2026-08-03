import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.user import Visibility
from app.services.graph_service import ALLOWED_EDGE_LABELS


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


class EdgeCreate(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    label: str = "LINKS_TO"

    @field_validator("label")
    @classmethod
    def _label_allowed(cls, v: str) -> str:
        """Reject unknown labels at validation time (422) — the label is
        interpolated into Cypher, so only the fixed vocabulary may pass."""
        if v not in ALLOWED_EDGE_LABELS:
            raise ValueError("unknown edge label")
        return v


class EdgeDelete(EdgeCreate):
    """Same shape as EdgeCreate: (source_id, target_id, label) identifies an edge."""


class EdgeOut(BaseModel):
    source_id: uuid.UUID
    target_id: uuid.UUID
    label: str
