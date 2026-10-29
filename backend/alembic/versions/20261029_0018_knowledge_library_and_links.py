"""knowledge: research library, document provenance, run-linked knowledge bases

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-29 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_bases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=16), nullable=False, server_default="user"))
    with op.batch_alter_table("knowledge_documents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("title", sa.String(length=300), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("origin", sa.String(length=16), nullable=False, server_default="upload"))
        batch_op.add_column(sa.Column("source_url", sa.String(length=2000), nullable=True))
        batch_op.add_column(sa.Column("url_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("run_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_knowledge_documents_run_id_runs"), "runs", ["run_id"], ["id"], ondelete="SET NULL"
        )
        # Library capture dedupes by (kb_id, url_hash).
        batch_op.create_index(batch_op.f("ix_knowledge_documents_url_hash"), ["url_hash"], unique=False)
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "knowledge_base_ids",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("knowledge_base_ids")
    with op.batch_alter_table("knowledge_documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_knowledge_documents_url_hash"))
        batch_op.drop_constraint(batch_op.f("fk_knowledge_documents_run_id_runs"), type_="foreignkey")
        batch_op.drop_column("run_id")
        batch_op.drop_column("url_hash")
        batch_op.drop_column("source_url")
        batch_op.drop_column("origin")
        batch_op.drop_column("title")
    with op.batch_alter_table("knowledge_bases", schema=None) as batch_op:
        batch_op.drop_column("kind")
