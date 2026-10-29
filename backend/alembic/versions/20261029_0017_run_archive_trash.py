"""run archive and trash: archived_at, trashed_at

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-29 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
        # Trash purge (retention) scans by trashed_at.
        batch_op.create_index(batch_op.f("ix_runs_trashed_at"), ["trashed_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_trashed_at"))
        batch_op.drop_column("trashed_at")
        batch_op.drop_column("archived_at")
