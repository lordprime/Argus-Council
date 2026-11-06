"""M4: /insights counts sub-council spend once and never loads full payloads or run bodies."""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import event

from argus.db.models import Run, RunEvent


async def _me(client, auth) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me", headers=auth)).json()["id"])


def _thought(run_id, seq, *, usd, text="x" * 10_000):
    return RunEvent(
        run_id=run_id,
        seq=seq,
        event_id=f"e{uuid.uuid4().hex[:12]}",
        type="thought.completed",
        ts=datetime.now(UTC),
        payload={
            "type": "thought.completed",
            "data": {
                "text": text,
                "provider": "p",
                "model": "m",
                "cost_usd": usd,
                "tokens": {"in": 10, "out": 5},
                "latency_ms": 100,
            },
        },
    )


async def _seed(app, owner):
    async with app.state.db.sessionmaker() as s:
        parent = Run(
            owner_id=owner,
            topic="parent",
            mode="research",
            status="completed",
            usage={"usd": 0.13, "tokens_in": 30, "tokens_out": 15, "runtime_s": 10},
            result="R" * 50_000,
            council_snapshot={"members": ["big"] * 1000},
        )
        s.add(parent)
        await s.flush()
        child = Run(
            owner_id=owner,
            parent_run_id=parent.id,
            topic="child",
            mode="research",
            status="completed",
            usage={"usd": 0.08, "tokens_in": 20, "tokens_out": 10, "runtime_s": 4},
        )
        s.add(child)
        await s.flush()
        s.add_all(
            [
                _thought(parent.id, 1, usd=0.05),
                _thought(child.id, 1, usd=0.08),
                RunEvent(
                    run_id=child.id,
                    seq=2,
                    event_id="e-tool-start",
                    type="tool.call.started",
                    ts=datetime.now(UTC),
                    payload={"data": {"tool_call_id": "c1", "tool": "web_search"}},
                ),
                RunEvent(
                    run_id=child.id,
                    seq=3,
                    event_id="e-tool-done",
                    type="tool.call.completed",
                    ts=datetime.now(UTC),
                    payload={"data": {"tool_call_id": "c1", "status": "error", "duration_ms": 40}},
                ),
            ]
        )
        await s.commit()
    return parent, child


async def test_insights_counts_sub_council_spend_once(client, auth, app):
    await _seed(app, await _me(client, auth))
    data = (await client.get("/api/v1/insights", headers=auth)).json()
    # The parent's usage already includes the child's charge-back.
    assert data["totals"]["cost_usd"] == 0.13
    assert data["totals"]["runs"] == 1 and data["analysed_runs"] == 1
    assert data["totals"]["tokens_in"] == 30
    assert data["runs_by_mode"] == {"research": 1}
    # Per-model and per-tool breakdowns still include what the sub-council did.
    model = next(m for m in data["models"] if m["model"] == "p/m")
    assert model["calls"] == 2 and model["cost_usd"] == 0.13 and model["tokens"] == 30
    tool = next(t for t in data["tools"] if t["tool"] == "web_search")
    assert tool["calls"] == 1 and tool["error_rate"] == 100.0 and tool["avg_ms"] == 40


async def test_insights_does_not_load_payloads_or_run_bodies(client, auth, app):
    await _seed(app, await _me(client, auth))
    statements: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = app.state.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        assert (await client.get("/api/v1/insights", headers=auth)).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", before)
    selects = [s for s in statements if "FROM runs" in s or "FROM run_events" in s]
    assert selects
    for sql in selects:
        cols = sql.split("FROM")[0]
        assert "runs.result" not in cols and "runs.council_snapshot" not in cols, sql
        # The payload may only be read through JSON path expressions, never as a whole column.
        # SQLite renders a path read as json_extract(run_events.payload, ...), Postgres as
        # run_events.payload #>> ... (or ->> / -> for a single key).
        bare = re.sub(
            r"json_extract\(run_events\.payload|run_events\.payload\s*(#>>|->>|->|#>)",
            "",
            cols,
            flags=re.IGNORECASE,
        )
        assert "run_events.payload" not in bare, sql


async def test_insights_compares_periods_and_explains_tool_errors(client, auth, app):
    from datetime import timedelta

    owner = await _me(client, auth)
    now = datetime.now(UTC)
    async with app.state.db.sessionmaker() as s:
        current = Run(owner_id=owner, topic="now", mode="research", status="failed", usage={"usd": 0.5})
        earlier = Run(
            owner_id=owner,
            topic="last week",
            mode="research",
            status="completed",
            usage={"usd": 0.2},
            created_at=now - timedelta(days=10),
        )
        s.add_all([current, earlier])
        await s.flush()

        def ev(seq, etype, data):
            return RunEvent(
                run_id=current.id,
                seq=seq,
                event_id=f"e{uuid.uuid4().hex[:12]}",
                type=etype,
                ts=now,
                payload={"data": data},
            )

        s.add_all(
            [
                ev(1, "tool.call.started", {"tool_call_id": "a", "tool": "web_fetch"}),
                ev(
                    2,
                    "tool.call.completed",
                    {
                        "tool_call_id": "a",
                        "status": "error",
                        "result_preview": "Fetch blocked: HTTP 404 from www.example.com",
                    },
                ),
                ev(3, "tool.call.started", {"tool_call_id": "b", "tool": "web_fetch"}),
                ev(
                    4,
                    "tool.call.completed",
                    {
                        "tool_call_id": "b",
                        "status": "error",
                        "result_preview": "Fetch blocked: HTTP 404 from news.example.org",
                    },
                ),
                ev(5, "approval.resolved", {"decision": "approved", "by": "auto"}),
                ev(6, "approval.resolved", {"decision": "approved"}),
            ]
        )
        await s.commit()

    data = (await client.get("/api/v1/insights?days=7", headers=auth)).json()
    assert data["totals"]["runs"] == 1 and data["previous_totals"]["runs"] == 1
    assert data["previous_totals"]["cost_usd"] == 0.2 and data["previous_totals"]["success_rate"] == 100.0
    # One bucket per day of the window (plus today's partial day), the run counted once.
    assert len(data["series"]) in (7, 8)
    assert sum(b["runs"] for b in data["series"]) == 1 and sum(b["failed"] for b in data["series"]) == 1
    fetch = next(t for t in data["tools"] if t["tool"] == "web_fetch")
    # Hosts and numbers are masked, so the two 404s group as one reason.
    assert fetch["top_errors"] == [{"error": "Fetch blocked: HTTP 404 from www.example.com", "count": 2}]
    assert data["oversight"]["approvals"] == {"approved": 2} and data["oversight"]["approvals_auto"] == 1
