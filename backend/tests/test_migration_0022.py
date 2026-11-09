"""Migration 0022 deletes ACP remote agents and leaves the hash-chained ledger untouched."""

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261109_0022_remove_acp.py"

_meta = sa.MetaData()
remote_agents = sa.Table(
    "remote_agents",
    _meta,
    sa.Column("owner_id", sa.Uuid(), nullable=False),
    sa.Column("name", sa.String(40), nullable=False),
    sa.Column("protocol", sa.String(8), nullable=False),
    sa.Column("url", sa.String(500), nullable=False),
    sa.Column("agent_name", sa.String(120), nullable=True),
    sa.Column("token_enc", sa.Text(), nullable=True),
    sa.Column("card", sa.JSON(), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)
ledger_entries = sa.Table(
    "ledger_entries",
    _meta,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("owner_id", sa.Uuid(), nullable=False),
    sa.Column("seq", sa.Integer(), nullable=False),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    sa.Column("protocol", sa.String(8), nullable=False),
    sa.Column("direction", sa.String(8), nullable=False),
    sa.Column("peer", sa.String(500), nullable=False),
    sa.Column("method", sa.String(120), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("response_sha256", sa.String(64), nullable=False),
    sa.Column("latency_ms", sa.Integer(), nullable=False),
    sa.Column("run_id", sa.Uuid(), nullable=True),
    sa.Column("detail", sa.JSON(), nullable=False),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("hash", sa.String(64), nullable=False),
)


@pytest.fixture
def sync_engine_with_0021_schema():
    engine = sa.create_engine("sqlite://")
    _meta.create_all(engine)
    yield engine
    engine.dispose()


def _load():
    spec = importlib.util.spec_from_file_location("migration_0022", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_purge_acp_agents_keeps_a2a_and_acp_ledger_rows(sync_engine_with_0021_schema):
    owner = uuid.uuid4()
    now = datetime.now(UTC)
    with sync_engine_with_0021_schema.begin() as conn:
        for name, protocol in (("keep", "a2a"), ("gone", "acp")):
            conn.execute(
                remote_agents.insert().values(
                    owner_id=owner,
                    name=name,
                    protocol=protocol,
                    url="https://agent.example.com",
                    card={},
                    description="",
                    enabled=True,
                    id=uuid.uuid4(),
                    created_at=now,
                    updated_at=now,
                )
            )
        conn.execute(
            ledger_entries.insert().values(
                owner_id=owner,
                seq=1,
                ts=now,
                protocol="acp",
                direction="outbound",
                peer="https://agent.example.com",
                method="run",
                status="ok",
                request_sha256="0" * 64,
                response_sha256="0" * 64,
                latency_ms=0,
                detail={},
                prev_hash="0" * 64,
                hash="1" * 64,
            )
        )
        names = _load().purge_acp_agents(conn)
        assert names == ["gone"]
        assert [r.name for r in conn.execute(sa.select(remote_agents)).all()] == ["keep"]
        count = conn.execute(
            sa.select(sa.func.count()).select_from(ledger_entries).where(ledger_entries.c.protocol == "acp")
        ).scalar()
        assert count == 1
