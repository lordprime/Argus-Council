"""L3: the run list uses a composite index, skips heavy columns and pages without skipping ties."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import event, select

from argus.db.models import Run


async def _me(client, auth) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me", headers=auth)).json()["id"])


async def test_run_list_pages_through_identical_timestamps(client, auth, app):
    owner = await _me(client, auth)
    ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    async with app.state.db.sessionmaker() as s:
        s.add_all([Run(owner_id=owner, topic=f"t{i}", status="completed", created_at=ts) for i in range(5)])
        await s.commit()
    seen: list[str] = []
    params: dict = {"limit": 2}
    for _ in range(5):
        page = (await client.get("/api/v1/runs", headers=auth, params=params)).json()
        if not page:
            break
        seen += [r["id"] for r in page]
        params = {"limit": 2, "before": page[-1]["created_at"], "before_id": page[-1]["id"]}
    assert len(seen) == 5 and len(set(seen)) == 5


async def test_run_list_does_not_load_result_or_snapshot(client, auth, app):
    owner = await _me(client, auth)
    async with app.state.db.sessionmaker() as s:
        s.add(Run(owner_id=owner, topic="big", result="R" * 10_000, council_snapshot={"x": "y" * 10_000}))
        await s.commit()
    statements: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = app.state.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        body = (await client.get("/api/v1/runs", headers=auth)).json()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert body[0]["topic"] == "big"
    sql = next(s for s in statements if "FROM runs" in s)
    cols = sql.split("FROM")[0]
    assert "runs.result" not in cols and "runs.council_snapshot" not in cols


async def test_run_list_query_uses_the_composite_index(app):
    owner = uuid.uuid4()
    q = (
        select(Run.id)
        .where(Run.owner_id == owner, Run.parent_run_id.is_(None))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(50)
    )
    async with app.state.db.engine.connect() as conn:
        if conn.dialect.name != "sqlite":
            return  # the plan check is SQLite-specific; the index itself is dialect-neutral
        compiled = q.compile(dialect=conn.dialect)
        args = tuple(
            owner.hex if k.startswith("owner_id") else compiled.params[k] for k in compiled.positiontup
        )
        plan = (await conn.exec_driver_sql("EXPLAIN QUERY PLAN " + str(compiled), args)).all()
    assert any("ix_runs_owner_parent_created" in str(row) for row in plan), plan
