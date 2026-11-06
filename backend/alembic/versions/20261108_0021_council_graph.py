"""graph_nodes and graph_edges: the council knowledge graph

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-08 12:00:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _scope(table: str) -> list[Any]:
    """owner, council and run columns with their foreign keys (as ``memory_items`` has them)."""
    return [
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("council_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f(f"fk_{table}_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["council_id"], ["councils.id"], name=op.f(f"fk_{table}_council_id_councils"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f(f"fk_{table}_run_id_runs"), ondelete="SET NULL"
        ),
    ]


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "graph_nodes",
        *_scope("graph_nodes"),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("terms", _json(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("meta", _json(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_nodes")),
    )
    for col in ("owner_id", "council_id", "run_id", "key"):
        op.create_index(op.f(f"ix_graph_nodes_{col}"), "graph_nodes", [col])

    op.create_table(
        "graph_edges",
        *_scope("graph_edges"),
        sa.Column("src", sa.String(length=64), nullable=False),
        sa.Column("dst", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=24), nullable=False),
        sa.Column("author", sa.String(length=120), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_edges")),
    )
    for col in ("owner_id", "council_id", "run_id"):
        op.create_index(op.f(f"ix_graph_edges_{col}"), "graph_edges", [col])


def downgrade() -> None:
    for col in ("owner_id", "council_id", "run_id"):
        op.drop_index(op.f(f"ix_graph_edges_{col}"), table_name="graph_edges")
    op.drop_table("graph_edges")
    for col in ("owner_id", "council_id", "run_id", "key"):
        op.drop_index(op.f(f"ix_graph_nodes_{col}"), table_name="graph_nodes")
    op.drop_table("graph_nodes")
