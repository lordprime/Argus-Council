"""api key hash scheme: hash_scheme

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-05 12:00:00.000000

API keys were stored as plain SHA-256 before 0.1.2 and are PBKDF2-SHA256 from then on. A stored
hash cannot be converted without the key itself, so every existing row is marked "sha256": such a
key keeps working, and its first use re-hashes it and marks it "pbkdf2".
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("hash_scheme", sa.String(length=16), nullable=False, server_default="sha256")
        )
    # The server default only backfills existing rows; new keys get their scheme from the model.
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.alter_column("hash_scheme", server_default=None)


def downgrade() -> None:
    # Keys already re-hashed with PBKDF2 stop matching under the older code, and match again once
    # upgraded (the PBKDF2 lookup comes first, whatever the backfilled scheme says).
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_column("hash_scheme")
