"""remove ACP: delete ACP remote agents and drop remote_agents.agent_name

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-09 12:00:00.000000
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


def purge_acp_agents(conn: sa.Connection) -> list[str]:
    """Delete remote agents that speak ACP and return their names. Ledger rows are never touched."""
    table = sa.table("remote_agents", sa.column("name", sa.String), sa.column("protocol", sa.String))
    names = [r[0] for r in conn.execute(sa.select(table.c.name).where(table.c.protocol == "acp"))]
    conn.execute(sa.delete(table).where(table.c.protocol == "acp"))
    return names


def upgrade() -> None:
    names = purge_acp_agents(op.get_bind())
    log.info("0022: deleted ACP remote agents: %s", ", ".join(names) or "none")
    with op.batch_alter_table("remote_agents", schema=None) as batch_op:
        batch_op.drop_column("agent_name")


def downgrade() -> None:
    with op.batch_alter_table("remote_agents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_name", sa.String(length=120), nullable=True))
