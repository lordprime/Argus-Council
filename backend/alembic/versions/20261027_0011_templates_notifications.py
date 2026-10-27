"""run templates and in-app notifications

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "run_templates",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("topic", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("council_id", sa.Uuid(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("outputs", JSON, nullable=False),
        sa.Column("guardrails", JSON, nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["council_id"],
            ["councils.id"],
            name=op.f("fk_run_templates_council_id_councils"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_run_templates_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_templates")),
        sa.UniqueConstraint("owner_id", "name", name=op.f("uq_run_templates_owner_id")),
    )
    with op.batch_alter_table("run_templates", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_templates_owner_id"), ["owner_id"], unique=False)

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("data", JSON, nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_notifications_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_notifications_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_read_at"), ["read_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notifications_read_at"))
        batch_op.drop_index(batch_op.f("ix_notifications_run_id"))
        batch_op.drop_index(batch_op.f("ix_notifications_user_id"))
    op.drop_table("notifications")
    with op.batch_alter_table("run_templates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_templates_owner_id"))
    op.drop_table("run_templates")
