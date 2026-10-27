"""composite run-list index, drop redundant run and event indexes

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-27 18:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_runs_owner_parent_created", ["owner_id", "parent_run_id", "created_at", "id"], unique=False
        )
        # The composite index leads with owner_id.
        batch_op.drop_index(batch_op.f("ix_runs_owner_id"))
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        # Covered by uq_run_events_run_id (run_id, seq); type alone is never queried.
        batch_op.drop_index(batch_op.f("ix_run_events_run_id"))
        batch_op.drop_index(batch_op.f("ix_run_events_type"))


def downgrade() -> None:
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_events_type"), ["type"], unique=False)
        batch_op.create_index(batch_op.f("ix_run_events_run_id"), ["run_id"], unique=False)
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_owner_id"), ["owner_id"], unique=False)
        batch_op.drop_index("ix_runs_owner_parent_created")
