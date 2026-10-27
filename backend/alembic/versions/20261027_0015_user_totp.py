"""optional TOTP second factor on users

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-27 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("totp_secret_enc", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("totp_recovery", json_type, server_default="[]", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("totp_recovery")
        batch_op.drop_column("totp_enabled")
        batch_op.drop_column("totp_secret_enc")
