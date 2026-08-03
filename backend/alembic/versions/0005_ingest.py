"""ingestion_runs and api_tokens

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# [plan-fix] plan created the enum via run_status.create() AND reused the same
# sa.Enum in create_table, which emits CREATE TYPE twice (DuplicateObject).
# Established pattern (0002): explicit create + create_type=False in the column.
run_status_enum = postgresql.ENUM(
    "pending", "running", "done", "failed", name="run_status", create_type=False
)


def upgrade() -> None:
    run_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", run_status_enum, nullable=False, server_default="pending"),
        sa.Column("total_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("processed_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("failed_items", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error_log", sa.Text),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index(op.f("ix_ingestion_runs_owner_id"), "ingestion_runs", ["owner_id"])

    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("scopes", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index(op.f("ix_api_tokens_owner_id"), "api_tokens", ["owner_id"])


def downgrade() -> None:
    op.drop_table("api_tokens")
    op.drop_table("ingestion_runs")
    run_status_enum.drop(op.get_bind(), checkfirst=True)
