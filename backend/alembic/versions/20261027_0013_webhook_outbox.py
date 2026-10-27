"""durable webhook outbox

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-27 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("webhook_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=60), nullable=False),
        sa.Column("delivery_id", sa.String(length=40), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["webhook_id"],
            ["webhooks.id"],
            name=op.f("fk_webhook_outbox_webhook_id_webhooks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_outbox")),
    )
    with op.batch_alter_table("webhook_outbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_webhook_outbox_webhook_id"), ["webhook_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_webhook_outbox_next_attempt_at"), ["next_attempt_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("webhook_outbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_outbox_next_attempt_at"))
        batch_op.drop_index(batch_op.f("ix_webhook_outbox_webhook_id"))
    op.drop_table("webhook_outbox")
