"""node_chunks with pgvector HNSW index

Revision ID: 0004
Revises: 38ca9223b637
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
# [plan-fix] plan said down_revision "0003"; the actual head is 38ca9223b637 (knowledge_core).
down_revision: str | Sequence[str] | None = "38ca9223b637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [plan-fix] fresh databases need the extension before Vector columns / HNSW (ADR-003).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "node_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("node_id", "chunk_index", name="uq_chunk_node_idx"),
    )
    # [plan-fix] no single-column ix_node_chunks_node_id: uq_chunk_node_idx leads with node_id
    # (same redundant-index removal applied across phase-1 models).
    # HNSW index for cosine similarity (ADR-003)
    op.execute("""
        CREATE INDEX ix_node_chunks_embedding_hnsw
        ON node_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.drop_index("ix_node_chunks_embedding_hnsw")
    op.drop_table("node_chunks")
