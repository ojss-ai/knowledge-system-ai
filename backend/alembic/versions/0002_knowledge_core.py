"""knowledge_core

Revision ID: 38ca9223b637
Revises: 275d5f4b90c3
Create Date: 2026-07-20 14:07:58.672694

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "38ca9223b637"
down_revision: str | Sequence[str] | None = "275d5f4b90c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enum type "visibility" already exists (created in 275d5f4b90c3); do not re-create.
visibility_enum = postgresql.ENUM(
    "private", "public", "shared", name="visibility", create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tags",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tags_slug"), "tags", ["slug"], unique=True)
    op.create_table(
        "knowledge_nodes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("node_type", sa.String(length=64), nullable=False),
        sa.Column("visibility", visibility_enum, nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("source_ref", sa.String(length=1024), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "body_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_ref", name="uq_node_source_ref"),
    )
    op.create_index(
        "ix_kn_owner_deleted", "knowledge_nodes", ["owner_id", "deleted_at"], unique=False
    )
    op.create_index(
        "ix_kn_tsv", "knowledge_nodes", ["body_tsv"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        op.f("ix_knowledge_nodes_owner_id"), "knowledge_nodes", ["owner_id"], unique=False
    )
    op.create_table(
        "node_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title_snapshot", sa.String(length=512), nullable=False),
        sa.Column("body_snapshot", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "version", name="uq_revision_version"),
    )
    op.create_index(op.f("ix_node_revisions_node_id"), "node_revisions", ["node_id"], unique=False)
    op.create_table(
        "node_shares",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("group_id", sa.UUID(), nullable=True),
        sa.Column("can_edit", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "group_id", name="uq_share_node_group"),
        sa.UniqueConstraint("node_id", "user_id", name="uq_share_node_user"),
        sa.CheckConstraint(
            "(user_id IS NULL) != (group_id IS NULL)", name="ck_node_shares_user_xor_group"
        ),
    )
    op.create_index(op.f("ix_node_shares_node_id"), "node_shares", ["node_id"], unique=False)
    op.create_table(
        "node_tags",
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("tag_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id", "tag_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("node_tags")
    op.drop_index(op.f("ix_node_shares_node_id"), table_name="node_shares")
    op.drop_table("node_shares")
    op.drop_index(op.f("ix_node_revisions_node_id"), table_name="node_revisions")
    op.drop_table("node_revisions")
    op.drop_index(op.f("ix_knowledge_nodes_owner_id"), table_name="knowledge_nodes")
    op.drop_index("ix_kn_tsv", table_name="knowledge_nodes", postgresql_using="gin")
    op.drop_index("ix_kn_owner_deleted", table_name="knowledge_nodes")
    op.drop_table("knowledge_nodes")
    op.drop_index(op.f("ix_tags_slug"), table_name="tags")
    op.drop_table("tags")
