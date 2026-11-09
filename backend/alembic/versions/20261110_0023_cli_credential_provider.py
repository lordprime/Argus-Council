"""cli credentials: which model provider a harness CLI's API key belongs to

Qwen Code, opencode and Goose run models from several providers, so their per-user API key is
stored with the provider it is for. Claude Code, Codex, Gemini CLI, Copilot, Kiro and Vibe keep
it empty.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-10 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cli_credentials", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provider", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cli_credentials", schema=None) as batch_op:
        batch_op.drop_column("provider")
