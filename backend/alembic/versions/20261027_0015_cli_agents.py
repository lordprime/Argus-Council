"""CLI agents: per-user CLI API keys and vendor CLI sessions

Revision ID: 0015_cli_agents
Revises: 0014
Create Date: 2026-10-27 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_cli_agents"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cli_credentials",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("cli", sa.String(length=16), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=False),
        sa.Column("key_hint", sa.String(length=32), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_cli_credentials_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cli_credentials")),
        sa.UniqueConstraint("owner_id", "cli", name=op.f("uq_cli_credentials_owner_id")),
    )
    with op.batch_alter_table("cli_credentials", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cli_credentials_owner_id"), ["owner_id"], unique=False)

    op.create_table(
        "cli_sessions",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("cli", sa.String(length=16), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("member_id", sa.String(length=64), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_cli_sessions_owner_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_cli_sessions_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cli_sessions")),
        sa.UniqueConstraint("owner_id", "cli", "session_id", name=op.f("uq_cli_sessions_owner_id")),
    )
    with op.batch_alter_table("cli_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cli_sessions_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_cli_sessions_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("cli_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cli_sessions_run_id"))
        batch_op.drop_index(batch_op.f("ix_cli_sessions_owner_id"))
    op.drop_table("cli_sessions")
    with op.batch_alter_table("cli_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cli_credentials_owner_id"))
    op.drop_table("cli_credentials")
