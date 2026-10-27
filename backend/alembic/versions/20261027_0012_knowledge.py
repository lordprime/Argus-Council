"""knowledge bases, documents, chunks and memory items

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_knowledge_bases_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
        sa.UniqueConstraint("owner_id", "name", name=op.f("uq_knowledge_bases_owner_id")),
    )
    op.create_index(op.f("ix_knowledge_bases_owner_id"), "knowledge_bases", ["owner_id"])

    op.create_table(
        "knowledge_documents",
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_documents_kb_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
    )
    op.create_index(op.f("ix_knowledge_documents_kb_id"), "knowledge_documents", ["kb_id"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("terms", _json(), nullable=False),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_chunks_kb_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            name=op.f("fk_knowledge_chunks_document_id_knowledge_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
    )
    op.create_index(op.f("ix_knowledge_chunks_kb_id"), "knowledge_chunks", ["kb_id"])
    op.create_index(op.f("ix_knowledge_chunks_document_id"), "knowledge_chunks", ["document_id"])

    op.create_table(
        "memory_items",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("council_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tags", _json(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_stamps(),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_memory_items_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["council_id"],
            ["councils.id"],
            name=op.f("fk_memory_items_council_id_councils"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_memory_items_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    op.create_index(op.f("ix_memory_items_owner_id"), "memory_items", ["owner_id"])
    op.create_index(op.f("ix_memory_items_council_id"), "memory_items", ["council_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_memory_items_council_id"), table_name="memory_items")
    op.drop_index(op.f("ix_memory_items_owner_id"), table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index(op.f("ix_knowledge_chunks_document_id"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_kb_id"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_documents_kb_id"), table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index(op.f("ix_knowledge_bases_owner_id"), table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
