"""H3: blocking MCP/A2A/ACP calls wait without holding pooled DB connections, and are bounded."""

from __future__ import annotations

import asyncio
import time

import pytest

from argus.core.metrics import REGISTRY
from argus.providers.mock import MockProvider, council_responder
from tests.test_protocols import rpc


class _Gated(MockProvider):
    """Every completion waits until the test opens the gate (runs stay mid-flight, holding nothing)."""

    def __init__(self) -> None:
        super().__init__("mock", responder=council_responder)
        self.gate = asyncio.Event()
        self.waiting = 0

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self.waiting += 1
        try:
            await self.gate.wait()
        finally:
            self.waiting -= 1
        return await super().complete(request)


@pytest.fixture
def gated(app):
    provider = _Gated()
    original = app.state.llm.system["mock"]
    app.state.llm.system["mock"] = provider
    yield provider
    provider.gate.set()
    app.state.llm.system["mock"] = original


@pytest.fixture
async def api_key(client, auth):
    return (await client.post("/api/v1/auth/api-keys", headers=auth, json={"name": "proto"})).json()["key"]


def _start(client, key, wait: float):
    return rpc(
        client,
        key,
        "tools/call",
        {
            "name": "start_run",
            "arguments": {"topic": "wait", "mode": "chat", "size": 2, "wait_seconds": wait},
        },
    )


async def _until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _shed(protocol: str) -> float:
    return (
        REGISTRY.get_sample_value("argus_protocol_blocking_waits_shed_total", {"protocol": protocol}) or 0.0
    )


async def test_blocking_calls_release_their_db_connection(app, client, api_key, gated):
    pool = app.state.db.engine.pool
    calls = [asyncio.create_task(_start(client, api_key, 30)) for _ in range(5)]
    await _until(lambda: len(app.state.runs.live) == 5 and gated.waiting >= 5)
    samples = []
    for _ in range(10):
        samples.append(pool.checkedout())
        await asyncio.sleep(0.03)
    # Five callers are waiting; none of them (nor any 100 ms poller) holds a pooled connection.
    assert max(samples) == 0, samples
    assert REGISTRY.get_sample_value("argus_protocol_blocking_waits") == 5
    gated.gate.set()
    results = await asyncio.gather(*calls)
    for r in results:
        assert '"status": "completed"' in r.json()["result"]["content"][0]["text"]
    assert REGISTRY.get_sample_value("argus_protocol_blocking_waits") == 0


async def test_wait_on_live_run_does_not_poll_the_database(app, gated, client, auth):
    from argus.services.runs import wait_terminal

    run = (
        await client.post("/api/v1/runs", headers=auth, json={"topic": "p", "mode": "chat", "size": 2})
    ).json()
    opened = 0
    real = app.state.db.sessionmaker

    def counting():
        nonlocal opened
        opened += 1
        return real()

    import uuid

    waiter = asyncio.create_task(wait_terminal(counting, uuid.UUID(run["id"]), 30, manager=app.state.runs))
    await asyncio.sleep(1.0)
    assert opened <= 2  # the old loop opened ~10 sessions a second
    gated.gate.set()
    done = await asyncio.wait_for(waiter, 10)
    assert done.status == "completed"


async def test_wait_on_run_not_live_here_polls_until_terminal(app, client, auth):
    import uuid

    from argus.services.runs import wait_terminal

    run = (
        await client.post("/api/v1/runs", headers=auth, json={"topic": "q", "mode": "chat", "size": 2})
    ).json()
    finished = await wait_terminal(
        app.state.db.sessionmaker, uuid.UUID(run["id"]), 10, manager=None, poll_s=0.05
    )
    assert finished.status == "completed"


async def test_wait_limit_sheds_extra_blocking_calls(app, client, api_key, gated):
    from argus.services.runs import BlockingWaits

    app.state.blocking_waits = BlockingWaits(1)
    before = _shed("mcp")
    first = asyncio.create_task(_start(client, api_key, 30))
    await _until(lambda: app.state.blocking_waits.active == 1)
    t0 = time.monotonic()
    second = await _start(client, api_key, 30)
    assert time.monotonic() - t0 < 5
    text = second.json()["result"]["content"][0]["text"]
    assert '"status": "running"' in text or '"status": "queued"' in text
    assert _shed("mcp") == before + 1
    gated.gate.set()
    assert '"status": "completed"' in (await first).json()["result"]["content"][0]["text"]


async def test_a2a_wait_is_clamped_and_validated(app, client, api_key, gated):
    app.state.settings.protocol_max_wait_s = 0.3
    for timeout in (10_000, "not-a-number"):
        t0 = time.monotonic()
        r = await rpc(
            client,
            api_key,
            "message/send",
            {
                "message": {
                    "parts": [{"kind": "text", "text": "hi"}],
                    "metadata": {"mode": "chat", "timeout_s": timeout},
                },
                "configuration": {"blocking": True},
            },
            path="/a2a",
        )
        assert r.status_code == 200, r.text
        assert "result" in r.json(), r.json()
        assert time.monotonic() - t0 < 5
        assert r.json()["result"]["status"]["state"] in ("submitted", "working")


async def test_acp_sync_wait_uses_configured_cap(app, client, api_key, gated):
    app.state.settings.protocol_max_wait_s = 0.3
    t0 = time.monotonic()
    r = await client.post(
        "/acp/runs",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"agent_name": "argus-chat", "mode": "sync", "input": [{"parts": [{"content": "x"}]}]},
    )
    assert r.status_code == 200, r.text
    assert time.monotonic() - t0 < 5
    assert r.json()["status"] in ("created", "in-progress")
