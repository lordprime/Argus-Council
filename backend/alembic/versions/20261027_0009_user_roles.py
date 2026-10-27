"""user roles (RBAC): users.role replaces users.is_admin; users.last_login_at

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", sa.String(length=16), server_default="member", nullable=False))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_users_role"), ["role"], unique=False)
    # Backfill: existing administrators keep admin rights; everyone else becomes a member.
    op.execute(sa.text("UPDATE users SET role = 'admin' WHERE is_admin = :t").bindparams(t=True))
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("is_admin")


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute(sa.text("UPDATE users SET is_admin = :t WHERE role = 'admin'").bindparams(t=True))
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_role"))
        batch_op.drop_column("last_login_at")
        batch_op.drop_column("role")
