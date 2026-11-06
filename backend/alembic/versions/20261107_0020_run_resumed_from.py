"""runs.resumed_from_id: a resumed run points at the run it continues

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resumed_from_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_runs_resumed_from_id_runs", "runs", ["resumed_from_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_runs_resumed_from_id_runs", type_="foreignkey")
        batch_op.drop_column("resumed_from_id")
