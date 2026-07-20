from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.user import Visibility  # reuse the enum


class NodeType(enum.StrEnum):
    note = "note"
    daily_log = "daily_log"
    file = "file"
    code_file = "code_file"
    code_symbol = "code_symbol"
    confluence_page = "confluence_page"


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    node_type: Mapped[str] = mapped_column(String(64), nullable=False, default=NodeType.note.value)
    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, name="visibility"), nullable=False, default=Visibility.private
    )
    source: Mapped[str | None] = mapped_column(String(64))  # "md_upload", "confluence", "codebase"
    source_ref: Mapped[str | None] = mapped_column(String(1024))  # unique external key
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    body_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))",
            persisted=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shares: Mapped[list[NodeShare]] = relationship(
        "NodeShare", back_populates="node", cascade="all, delete-orphan"
    )
    revisions: Mapped[list[NodeRevision]] = relationship(
        "NodeRevision",
        back_populates="node",
        cascade="all, delete-orphan",
        order_by="NodeRevision.version.desc()",
    )
    node_tags: Mapped[list[NodeTag]] = relationship(
        "NodeTag", back_populates="node", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source", "source_ref", name="uq_node_source_ref"),
        Index("ix_kn_owner_deleted", "owner_id", "deleted_at"),
        Index("ix_kn_tsv", "body_tsv", postgresql_using="gin"),
    )


class NodeShare(Base):
    __tablename__ = "node_shares"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE")
    )
    can_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    node: Mapped[KnowledgeNode] = relationship("KnowledgeNode", back_populates="shares")

    __table_args__ = (
        UniqueConstraint("node_id", "user_id", name="uq_share_node_user"),
        UniqueConstraint("node_id", "group_id", name="uq_share_node_group"),
    )


class NodeRevision(Base):
    __tablename__ = "node_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(512), nullable=False)
    body_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    node: Mapped[KnowledgeNode] = relationship("KnowledgeNode", back_populates="revisions")

    __table_args__ = (UniqueConstraint("node_id", "version", name="uq_revision_version"),)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    node_tags: Mapped[list[NodeTag]] = relationship(
        "NodeTag", back_populates="tag", cascade="all, delete-orphan"
    )


class NodeTag(Base):
    __tablename__ = "node_tags"

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    node: Mapped[KnowledgeNode] = relationship("KnowledgeNode", back_populates="node_tags")
    tag: Mapped[Tag] = relationship("Tag", back_populates="node_tags")
