"""L6: run-scoped work logs under the run's id, not under the id of the request that started it."""

from __future__ import annotations

import structlog

from tests.test_runs import API, wait_status


class _ContextProbe:
    """Stands in for the webhook dispatcher; records the log context each event is emitted under."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []
        self.tasks: set = set()

    def emit(self, owner, event: str, data: dict) -> None:
        self.seen.append((event, dict(structlog.contextvars.get_contextvars())))

    async def drain(self) -> None:
        return None


async def test_run_task_logs_with_run_id_not_request_id(app, client, auth):
    probe = _ContextProbe()
    app.state.runs.webhooks = probe
    r = await client.post(
        API,
        headers={**auth, "X-Request-ID": "creating-req"},
        json={"topic": "ctx", "mode": "chat", "size": 2},
    )
    run = r.json()
    await wait_status(client, auth, run["id"])
    started = next(ctx for ev, ctx in probe.seen if ev == "run.started")
    assert started.get("run_id") == run["id"]
    assert started.get("mode") == "chat"
    assert "request_id" not in started
    # The request's own context is untouched.
    assert "run_id" not in structlog.contextvars.get_contextvars()


def test_fresh_log_context_isolated():
    from argus.core.logging import fresh_log_context

    structlog.contextvars.bind_contextvars(request_id="outer")
    try:
        ctx = fresh_log_context(run_id="r1")
        inner = ctx.run(structlog.contextvars.get_contextvars)
        assert inner == {"run_id": "r1"}
        assert structlog.contextvars.get_contextvars() == {"request_id": "outer"}
    finally:
        structlog.contextvars.clear_contextvars()
