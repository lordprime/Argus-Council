"""Run runtime reliability: finalisation, recovery, retries, cancellation, budgets and streaming."""

from __future__ import annotations

import asyncio
import json
import signal
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from argus.core.metrics import RUNS_ACTIVE, RUNS_FINISHED
from argus.db.models import Run, RunEvent, User
from argus.providers.base import ProviderError
from argus.providers.mock import MockProvider, council_responder
from argus.providers.types import CompletionResponse, StreamEvent, ToolCall, Usage
from argus.runtime.events import EventSink, MemoryBroker, load_events
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from tests.test_runs import (
    API,
    _tool_council,
    _tool_run_setup,
    _wait_for_event,
    events_of,
    install,
    types,
    wait_status,
)


def _db_down() -> OperationalError:
    return OperationalError("INSERT ...", {}, ConnectionError("server closed the connection"))


class FlakySessions:
    """Wraps a sessionmaker; while ``down`` every session fails to open, like a DB failover."""

    def __init__(self, real) -> None:
        self.real = real
        self.down = False

    def __call__(self):
        if self.down:
            return _Failing()
        return self.real()


class _Failing:
    async def __aenter__(self):
        raise _db_down()

    async def __aexit__(self, *exc):
        return False


async def _until_not_live(runs, run_id, timeout=5.0) -> None:
    """The row turns terminal just before the run's task finishes and leaves ``live``."""
    live = runs.live.get(uuid.UUID(str(run_id)))
    if live is not None and live.task is not None:
        await asyncio.wait_for(asyncio.shield(live.task), timeout)
    assert uuid.UUID(str(run_id)) not in runs.live


# ----------------------------------------------------------------------------- H1 / L2
async def test_transient_db_outage_does_not_leave_zombie_run(client, auth, app, monkeypatch):
    import argus.runtime.manager as manager_mod

    monkeypatch.setattr(manager_mod, "FINALIZE_DELAYS", (0.2, 0.3, 0.5, 1.0, 1.0))
    runs = app.state.runs
    flaky = FlakySessions(runs.sessionmaker)
    runs.sessionmaker = flaky
    loop = asyncio.get_running_loop()
    tripped = {"n": 0}

    def responder(req):
        if req.metadata.get("phase") == "execute" and not tripped["n"]:
            tripped["n"] += 1
            flaky.down = True
            loop.call_later(2.5, setattr, flaky, "down", False)  # outlasts the sink retries
        return council_responder(req)

    install(app, "dbflap", responder)
    active_before = RUNS_ACTIVE._value.get()
    r = await client.post(
        API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2, "provider": "dbflap", "model": "m"}
    )
    rid = r.json()["id"]
    run = await wait_status(client, auth, rid, timeout=20)
    assert run["status"] == "failed" and run["finished_at"]
    await _until_not_live(runs, rid)
    assert RUNS_ACTIVE._value.get() == active_before
    evs = await events_of(client, auth, rid)
    assert evs[-1]["type"] == "run.status" and evs[-1]["data"]["status"] == "failed"
    # A failed persist never leaves a permanent hole in the event log.
    seqs = [e["seq"] for e in evs]
    assert seqs == list(range(1, len(seqs) + 1))
    assert (await client.post(f"{API}/{rid}/cancel", headers=auth)).status_code == 202
    assert (await client.get(f"{API}/{rid}", headers=auth)).json()["status"] == "failed"


async def test_failure_marking_run_running_still_finalises(client, auth, app):
    runs = app.state.runs
    real_set = runs._set
    calls = {"n": 0}

    async def flaky_set(run_id, **values):
        if values.get("status") == "running" and not calls["n"]:
            calls["n"] += 1
            raise _db_down()
        return await real_set(run_id, **values)

    runs._set = flaky_set
    active_before = RUNS_ACTIVE._value.get()
    r = await client.post(API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2})
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "failed"
    await _until_not_live(runs, run["id"])
    assert RUNS_ACTIVE._value.get() == active_before


@pytest.fixture
async def run_row(app):
    async with app.state.db.sessionmaker() as s:
        u = User(email=f"{uuid.uuid4().hex}@x.io", password_hash="x")
        s.add(u)
        await s.flush()
        r = Run(owner_id=u.id, topic="t")
        s.add(r)
        await s.commit()
        return r


async def test_sink_retries_a_transient_persist_error(app, run_row):
    flaky = FlakySessions(app.state.db.sessionmaker)
    sink = EventSink(run_row.id, None, flaky, MemoryBroker())
    flaky.down = True
    asyncio.get_running_loop().call_later(0.05, setattr, flaky, "down", False)
    env = await sink.emit("run.status", {"status": "running"})
    assert env.seq == 1
    assert [e["seq"] for e in await load_events(app.state.db.sessionmaker, run_row.id)] == [1]


async def test_sink_rolls_back_seq_when_persist_fails(app, run_row):
    flaky = FlakySessions(app.state.db.sessionmaker)
    sink = EventSink(run_row.id, None, flaky, MemoryBroker())
    await sink.emit("run.status", {"status": "running"})
    flaky.down = True
    with pytest.raises(OperationalError):
        await sink.emit("step.status", {"step_id": "s1", "status": "running"})
    flaky.down = False
    env = await sink.emit("step.status", {"step_id": "s1", "status": "done"})
    assert env.seq == 2
    stored = await load_events(app.state.db.sessionmaker, run_row.id)
    assert [e["seq"] for e in stored] == [1, 2]


async def test_sink_refuses_durable_events_after_terminal_status(app, run_row):
    sink = EventSink(run_row.id, None, app.state.db.sessionmaker, MemoryBroker())
    await sink.emit("run.status", {"status": "running"})
    await sink.emit("run.status", {"status": "failed"})
    await sink.emit("vote.cast", {"vote_id": "v", "member_id": "m", "option_id": "approve"})
    stored = await load_events(app.state.db.sessionmaker, run_row.id)
    assert [e["type"] for e in stored] == ["run.status", "run.status"]


@pytest.fixture(params=["default", "fakeredis"])
async def sse_app(request, app):
    """The app with its own broker, and again with the shared-connection RedisBroker."""
    if request.param == "default":
        yield app
        return
    import fakeredis.aioredis

    from argus.runtime.events import RedisBroker

    previous = app.state.broker
    broker = RedisBroker(fakeredis.aioredis.FakeRedis())
    app.state.broker = app.state.runs.broker = broker
    yield app
    await broker.close()
    app.state.broker = app.state.runs.broker = previous


async def _own_row(app, client, auth, status="running") -> uuid.UUID:
    """A run row owned by the authenticated user that no live task drives."""
    me = (await client.get("/api/v1/auth/me", headers=auth)).json()
    async with app.state.db.sessionmaker() as s:
        r = Run(owner_id=uuid.UUID(me["id"]), topic="manual", status=status)
        s.add(r)
        await s.commit()
        return r.id


async def _collect(client, auth, rid, **params) -> list[dict]:
    frames = []
    async with client.stream("GET", f"{API}/{rid}/events/stream", params=params, headers=auth) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
    return frames


async def _until_subscribed(broker, rid, timeout=5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        # MemoryBroker keys subscribers by run id, RedisBroker (CI) by channel name.
        if broker._subs.get(str(rid)) or broker._subs.get(f"{getattr(broker, 'prefix', '')}{rid}"):
            await asyncio.sleep(0.05)  # let the replay finish
            return
        await asyncio.sleep(0.01)
    raise AssertionError("stream never subscribed")


async def test_sse_backfills_events_missing_from_the_live_feed(client, auth, sse_app):
    rid = await _own_row(sse_app, client, auth)
    task = asyncio.create_task(_collect(client, auth, rid))
    await _until_subscribed(sse_app.state.broker, rid)
    sink = EventSink(rid, None, sse_app.state.db.sessionmaker, MemoryBroker())  # persists, publishes nowhere
    e1 = await sink.emit("run.status", {"status": "running"})
    await sink.emit("step.status", {"step_id": "s1", "status": "running"})
    e3 = await sink.emit("run.status", {"status": "completed"})
    # The live feed delivers 1 and 3 but never 2 (e.g. out-of-order publishes on Redis).
    await sse_app.state.broker.publish(str(rid), e1.dumps())
    await sse_app.state.broker.publish(str(rid), e3.dumps())
    frames = await asyncio.wait_for(task, 5)
    assert [f["seq"] for f in frames] == [1, 2, 3]


# ----------------------------------------------------------------------------- H2
async def test_recover_writes_terminal_event_webhook_and_metric(app, run_row):
    sink = EventSink(run_row.id, None, app.state.db.sessionmaker, MemoryBroker())
    await sink.emit("run.status", {"status": "running"})
    await sink.emit("step.status", {"step_id": "s1", "status": "running"})
    async with app.state.db.sessionmaker() as s:
        await s.execute(update(Run).where(Run.id == run_row.id).values(status="running"))
        await s.commit()
    sent = []
    app.state.runs.webhooks.emit = lambda owner, event, data: sent.append((owner, event, data))
    finished = RUNS_FINISHED.labels(run_row.mode, "failed")._value.get()
    await app.state.runs.recover()
    stored = await load_events(app.state.db.sessionmaker, run_row.id)
    assert [e["seq"] for e in stored] == [1, 2, 3]
    assert stored[-1]["type"] == "run.status" and stored[-1]["data"]["status"] == "failed"
    assert "restart" in stored[-1]["data"]["reason"]
    async with app.state.db.sessionmaker() as s:
        row = await s.get(Run, run_row.id)
    assert row.status == "failed" and row.last_seq == 3
    assert [(o, e) for o, e, _ in sent] == [(run_row.owner_id, "run.failed")]
    assert RUNS_FINISHED.labels(run_row.mode, "failed")._value.get() == finished + 1


async def test_sse_streams_end_on_shutdown(client, auth, sse_app):
    rid = await _own_row(sse_app, client, auth)
    task = asyncio.create_task(_collect(client, auth, rid))
    await _until_subscribed(sse_app.state.broker, rid)
    sse_app.state.sse.shutdown()
    await asyncio.wait_for(task, 3)
    # Streams opened after shutdown started end straight after the replay.
    await asyncio.wait_for(_collect(client, auth, rid), 3)


async def test_sse_ends_when_run_row_is_terminal(client, auth, sse_app, monkeypatch):
    import argus.api.v1.runs as runs_api

    monkeypatch.setattr(runs_api, "HEARTBEAT_S", 0.1)
    rid = await _own_row(sse_app, client, auth)
    task = asyncio.create_task(_collect(client, auth, rid))
    await _until_subscribed(sse_app.state.broker, rid)
    async with sse_app.state.db.sessionmaker() as s:
        await s.execute(update(Run).where(Run.id == rid).values(status="failed"))
        await s.commit()
    await asyncio.wait_for(task, 3)


def test_shutdown_signal_chain_calls_previous_handler():
    from argus.api.sse import chain_shutdown_signals

    seen = []
    previous = signal.signal(signal.SIGUSR1, lambda s, f: seen.append("previous"))
    try:

        async def go():
            restore = chain_shutdown_signals(lambda: seen.append("hook"), (signal.SIGUSR1,))
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(0.01)
            restore()
            signal.raise_signal(signal.SIGUSR1)
            await asyncio.sleep(0.01)

        asyncio.run(go())
    finally:
        signal.signal(signal.SIGUSR1, previous)
    assert seen == ["previous", "hook", "previous"]


# ----------------------------------------------------------------------------- M9
async def test_sse_buffer_drops_ephemeral_and_ends_on_durable_overflow():
    from argus.api.sse import END, HEARTBEAT, SseHub

    hub = SseHub(maxsize=2)
    buf = hub.open()
    assert buf.offer(b"d1", ephemeral=False) and buf.offer(b"d2", ephemeral=False)
    assert buf.offer(b"delta", ephemeral=True)  # full: the ephemeral frame is dropped, stream continues
    assert not buf.offer(b"d3", ephemeral=False)  # full: a durable frame cannot be dropped -> end
    assert [await buf.get(0.01) for _ in range(3)] == [b"d1", b"d2", END]
    other = hub.open()
    assert await other.get(0.01) is HEARTBEAT
    hub.release(buf)
    hub.release(other)
    assert not hub.buffers


async def test_redis_broker_shares_one_connection_across_streams():
    import fakeredis.aioredis

    from argus.runtime.events import RedisBroker

    client = fakeredis.aioredis.FakeRedis()
    opened = []
    real_pubsub = client.pubsub

    def counting_pubsub(**kw):
        opened.append(1)
        return real_pubsub(**kw)

    client.pubsub = counting_pubsub
    broker = RedisBroker(client)

    async def consume(run_id):
        ready = asyncio.Event()
        agen = broker.subscribe(run_id, ready)
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.wait_for(ready.wait(), 2)
        return agen, task

    subs = [await consume("A"), await consume("A"), await consume("B")]
    await broker.publish("A", b"hello-a")
    await broker.publish("B", b"hello-b")
    results = [await asyncio.wait_for(t, 2) for _, t in subs]
    assert results == [b"hello-a", b"hello-a", b"hello-b"]
    assert len(opened) == 1
    for agen, _ in subs:
        await agen.aclose()
    await broker.close()


# ----------------------------------------------------------------------------- H5
def _sub_response(req, provider="sub"):
    out = council_responder(req)
    if req.metadata.get("phase") == "execute" and "subcouncil" in {t.name for t in req.tools or []}:
        if not any(m.role == "tool" for m in req.messages):
            out = {
                "content": "",
                "tool_calls": [
                    {
                        "name": "subcouncil",
                        "arguments": {"task": "Survey the sub-topic", "size": 2, "mode": "chat"},
                    }
                ],
            }
        else:
            out = {"content": "merged the sub-council's findings"}
    return CompletionResponse(
        provider=provider,
        model="m",
        content=out.get("content", ""),
        tool_calls=[ToolCall(id=f"call_{i}", **c) for i, c in enumerate(out.get("tool_calls") or [])],
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01),
    )


class SlowSub(MockProvider):
    """Every call takes ``delay``; the child's work phase can be parked on ``gate``."""

    def __init__(self, name, delay=0.0, gate: asyncio.Event | None = None):
        super().__init__(name)
        self.delay = delay
        self.gate = gate

    async def complete(self, request):
        child = request.metadata.get("topic") == "Survey the sub-topic"
        if child and self.gate is not None and request.metadata.get("phase") == "execute":
            await self.gate.wait()
        await asyncio.sleep(self.delay)
        return _sub_response(request, self.name)


async def _delegating_council(client, auth, provider, guardrails=None):
    council = {
        "name": f"delegating-{provider}",
        "mode": "chat",
        "guardrails": {"max_subcouncil_depth": 1, **(guardrails or {})},
        "members": [
            {"name": "Chair", "role": "chair", "provider": provider, "model": "m"},
            {
                "name": "Lead",
                "role": "worker",
                "provider": provider,
                "model": "m",
                "config": {"tools": ["subcouncil"]},
            },
        ],
    }
    c = await client.post("/api/v1/councils", headers=auth, json=council)
    assert c.status_code == 201, c.text
    return c.json()["id"]


async def test_subcouncil_is_not_killed_by_the_generic_tool_timeout(client, auth, app, monkeypatch):
    import argus.tools.base as tools_base

    monkeypatch.setattr(tools_base, "TOOL_TIMEOUT_S", 0.2)
    app.state.llm.system["slowsub"] = SlowSub("slowsub", delay=0.05)
    cid = await _delegating_council(client, auth, "slowsub")
    run = (await client.post(API, headers=auth, json={"topic": "big task", "council_id": cid})).json()
    assert (await wait_status(client, auth, run["id"], timeout=30))["status"] == "completed"
    evs = await events_of(client, auth, run["id"])
    done = next(e for e in evs if e["type"] == "subcouncil.completed")
    assert done["data"]["status"] == "completed"
    tool_done = [e for e in evs if e["type"] == "tool.call.completed"]
    assert tool_done and all(e["data"]["status"] == "ok" for e in tool_done)


def test_tools_declare_their_own_timeouts():
    from argus.tools.base import TOOL_TIMEOUT_S
    from argus.tools.builtin import builtin_tools
    from argus.tools.code import Shell
    from argus.tools.subcouncil import SubCouncilTool

    shell = Shell(sandbox=None)
    assert shell.timeout_for(Shell.Args(command="make", timeout_s=550)) > 550
    assert SubCouncilTool(manager=None).timeout_for(None) is None
    assert builtin_tools()[0].timeout_for(None) == TOOL_TIMEOUT_S


async def _child_of(app, parent_id) -> Run:
    async with app.state.db.sessionmaker() as s:
        return (await s.execute(select(Run).where(Run.parent_run_id == uuid.UUID(parent_id)))).scalar_one()


async def _wait_child_working(app, parent_id, timeout=10):
    for _ in range(int(timeout / 0.05)):
        async with app.state.db.sessionmaker() as s:
            child = (
                await s.execute(select(Run).where(Run.parent_run_id == uuid.UUID(parent_id)))
            ).scalar_one_or_none()
            if child is not None:
                n = (
                    await s.execute(
                        select(RunEvent.id).where(
                            RunEvent.run_id == child.id, RunEvent.type == "thought.completed"
                        )
                    )
                ).all()
                if n:
                    return child
        await asyncio.sleep(0.05)
    raise AssertionError("child council never started working")


async def test_cancelling_the_parent_stops_the_child_and_charges_its_spend(client, auth, app, monkeypatch):
    import argus.runtime.manager as manager_mod

    # The child is parked inside an LLM call, so it never reaches a checkpoint by itself: the
    # cancel must reach it through the parent and then the forced cancel after the grace period.
    monkeypatch.setattr(manager_mod, "CANCEL_GRACE_S", 0.3)
    gate = asyncio.Event()
    app.state.llm.system["gsub"] = SlowSub("gsub", gate=gate)
    cid = await _delegating_council(client, auth, "gsub")
    rid = (await client.post(API, headers=auth, json={"topic": "big task", "council_id": cid})).json()["id"]
    await _wait_child_working(app, rid)
    assert (await client.post(f"{API}/{rid}/cancel", headers=auth)).status_code == 202
    run = await wait_status(client, auth, rid, timeout=5)
    await _until_not_live(app.state.runs, rid)
    gate.set()
    assert run["error"] is None  # a user cancel, not a server shutdown
    child = await _child_of(app, rid)
    assert run["status"] == "cancelled" and child.status == "cancelled"
    assert child.usage["usd"] > 0
    evs = await events_of(client, auth, rid)
    done = next(e for e in evs if e["type"] == "subcouncil.completed")
    assert done["data"]["status"] == "cancelled"
    own = sum(e["data"]["cost_usd"] for e in evs if e["type"] == "thought.completed")
    assert run["usage"]["usd"] == pytest.approx(own + child.usage["usd"])


async def test_parent_task_cancellation_still_charges_child_spend(client, auth, app):
    gate = asyncio.Event()
    app.state.llm.system["gsub2"] = SlowSub("gsub2", gate=gate)
    cid = await _delegating_council(client, auth, "gsub2")
    rid = (await client.post(API, headers=auth, json={"topic": "big task", "council_id": cid})).json()["id"]
    await _wait_child_working(app, rid)
    await app.state.runs.shutdown()  # cancels every live task: parent and child alike
    gate.set()
    run = (await client.get(f"{API}/{rid}", headers=auth)).json()
    child = await _child_of(app, rid)
    assert run["status"] == "cancelled" and child.status == "cancelled"
    evs = await events_of(client, auth, rid)
    assert any(e["type"] == "subcouncil.completed" for e in evs)
    own = sum(e["data"]["cost_usd"] for e in evs if e["type"] == "thought.completed")
    assert run["usage"]["usd"] == pytest.approx(own + child.usage["usd"])


# ----------------------------------------------------------------------------- M3
def test_child_engines_share_the_parent_budget():
    parent = GuardrailEngine(Guardrails.resolve({"budget": {"usd": 1.0, "tokens": 1000}}))
    a = GuardrailEngine(Guardrails.resolve({"budget": {"usd": 1.0}}), parent=parent)
    b = GuardrailEngine(Guardrails.resolve({"budget": {"usd": 1.0}}), parent=parent)
    a.record_usage(Usage(input_tokens=100, output_tokens=0, cost_usd=0.6))
    assert a.check_limits() is None and b.check_limits() is None
    b.record_usage(Usage(input_tokens=100, output_tokens=0, cost_usd=0.6))
    # Neither child is over its own cap, but together they exhaust the parent.
    assert a.check_limits().rule == "budget.usd" and b.check_limits().rule == "budget.usd"
    assert parent.check_limits().rule == "budget.usd"
    assert parent.total_usage.cost_usd == pytest.approx(1.2)
    parent.settle_child(a)
    assert parent.usage.cost_usd == pytest.approx(0.6) and parent.total_usage.cost_usd == pytest.approx(1.2)
    a.record_usage(Usage(cost_usd=5.0))  # spend after settlement no longer reaches the parent
    assert parent.total_usage.cost_usd == pytest.approx(1.2)


def test_subcouncil_budget_left_accounts_for_running_children():
    from argus.tools.subcouncil import child_budget

    parent = GuardrailEngine(Guardrails.resolve({"budget": {"usd": 1.0, "tokens": 1000}}))
    parent.record_usage(Usage(input_tokens=300, output_tokens=0, cost_usd=0.25))
    running = GuardrailEngine(Guardrails.resolve({}), parent=parent)
    running.record_usage(Usage(input_tokens=200, output_tokens=0, cost_usd=0.5))
    usd, tokens = child_budget(parent, Guardrails.resolve({"budget": {"usd": 1.0, "tokens": 1000}}))
    assert usd == pytest.approx(0.25) and tokens == 500


# ----------------------------------------------------------------------------- H6
class FlakyStream(MockProvider):
    def __init__(self, name, *, fail_first=0, partial_then_fail=False, status=503, retryable=True, when=None):
        super().__init__(name)
        self.fail_first = fail_first
        self.partial_then_fail = partial_then_fail
        self.status = status
        self.retryable = retryable
        self.when = when or (lambda req: req.metadata.get("phase") == "execute")
        self.stream_calls = 0
        self.failed_calls = 0

    async def stream(self, request):
        if self.when(request):
            self.stream_calls += 1
            if self.partial_then_fail and not self.failed_calls:
                self.failed_calls += 1
                yield StreamEvent(type="text", text="PARTIAL ")
                raise ProviderError("connection reset", provider=self.name, retryable=True)
            if self.failed_calls < self.fail_first:
                self.failed_calls += 1
                raise ProviderError(
                    "overloaded", provider=self.name, status=self.status, retryable=self.retryable
                )
        async for ev in super().stream(request):
            yield ev


async def test_transient_stream_error_is_retried(client, auth, app):
    app.state.llm.retry_base_delay = 0.01
    p = FlakyStream("flaky503", fail_first=2)
    app.state.llm.system["flaky503"] = p
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "t", "mode": "chat", "size": 2, "provider": "flaky503", "model": "m"},
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    assert p.failed_calls == 2 and p.stream_calls == 3


async def test_stream_failing_after_partial_output_falls_back_to_complete(client, auth, app):
    app.state.llm.retry_base_delay = 0.01
    p = FlakyStream("flakymid", partial_then_fail=True)
    app.state.llm.system["flakymid"] = p
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "t", "mode": "chat", "size": 2, "provider": "flakymid", "model": "m"},
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    assert p.stream_calls == 1  # never re-streamed after output reached the user
    thoughts = [e for e in await events_of(client, auth, run["id"]) if e["type"] == "thought.completed"]
    assert thoughts and not any("PARTIAL" in t["data"]["text"] for t in thoughts)


async def test_non_retryable_step_error_fails_the_step_not_the_run(client, auth, app):
    app.state.llm.retry_base_delay = 0.01
    p = FlakyStream(
        "badstep",
        fail_first=99,
        status=400,
        retryable=False,
        when=lambda req: req.metadata.get("phase") == "execute" and req.metadata.get("role") == "researcher",
    )
    app.state.llm.system["badstep"] = p
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "t", "mode": "research", "size": 6, "provider": "badstep", "model": "m"},
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    assert p.stream_calls == 1  # a 400 is not retried
    steps = {}
    for e in await events_of(client, auth, run["id"]):
        if e["type"] == "step.status":
            steps[e["data"]["step_id"]] = e["data"]
    assert steps["s2"]["status"] == "failed" and "provider" in steps["s2"]["reason"]
    assert steps["s3"]["status"] == "skipped" and steps["s4"]["status"] == "skipped"


async def test_removed_provider_falls_back_when_fallbacks_exist(client, auth, app):
    council = {
        "name": "fallbacks",
        "mode": "chat",
        "members": [
            {
                "name": "Ann",
                "role": "assistant",
                "provider": "deleted-provider",
                "model": "m",
                "config": {"fallbacks": [{"provider": "mock", "model": "mock-1"}]},
            },
        ],
    }
    cid = (await client.post("/api/v1/councils", headers=auth, json=council)).json()["id"]
    rid = (await client.post(API, headers=auth, json={"topic": "t", "council_id": cid})).json()["id"]
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed", run


# ----------------------------------------------------------------------------- M1
async def test_cancel_reaches_a_pending_approval(client, auth, app):
    _tool_run_setup(app, "apcancel")
    cid = await _tool_council(client, auth, "apcancel", {"oversight": {"monitor_sensitivity": "off"}})
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    await _wait_for_event(client, auth, rid, "approval.requested")
    assert (await client.post(f"{API}/{rid}/cancel", headers=auth)).status_code == 202
    run = await wait_status(client, auth, rid, timeout=3)
    assert run["status"] == "cancelled"
    resolved = next(e for e in await events_of(client, auth, rid) if e["type"] == "approval.resolved")
    assert resolved["data"]["decision"] == "expired" and "cancel" in resolved["data"]["reason"]


async def test_approval_timeout_is_a_setting(client, auth, app):
    app.state.runs.settings.approval_timeout_s = 0.3
    _tool_run_setup(app, "aptimeout")
    cid = await _tool_council(client, auth, "aptimeout", {"oversight": {"monitor_sensitivity": "off"}})
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    run = await wait_status(client, auth, rid, timeout=5)
    assert run["status"] == "completed"
    resolved = next(e for e in await events_of(client, auth, rid) if e["type"] == "approval.resolved")
    assert resolved["data"]["decision"] == "expired"


async def test_approval_wait_is_capped_by_the_remaining_runtime(client, auth, app):
    _tool_run_setup(app, "apruntime")
    cid = await _tool_council(
        client, auth, "apruntime", {"oversight": {"monitor_sensitivity": "off"}, "max_runtime_minutes": 0.02}
    )
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    run = await wait_status(client, auth, rid, timeout=5)
    assert run["status"] == "halted" and "runtime" in run["error"].lower()


# ----------------------------------------------------------------------------- M2
async def test_a_failing_voter_stops_the_other_voters(client, auth, app):
    finished_votes = []

    class Voters(MockProvider):
        async def complete(self, request):
            if request.metadata.get("phase") == "vote":
                if request.metadata.get("role") == "critic":
                    await asyncio.sleep(0.05)
                    raise ProviderError("bad request", provider=self.name, status=400)
                await asyncio.sleep(0.6)
                finished_votes.append(request.metadata.get("role"))
            return self._build(request, council_responder(request))

    app.state.llm.system["voters"] = Voters("voters")
    council = {
        "name": "voting",
        "mode": "chat",
        "guardrails": {"oversight": {"monitor_sensitivity": "off"}},
        "members": [
            {"name": "Ann", "role": "assistant", "provider": "voters", "model": "m"},
            {"name": "Chair", "role": "chair", "provider": "voters", "model": "m"},
            {"name": "Crit", "role": "critic", "provider": "voters", "model": "m"},
            {"name": "Al", "role": "analyst", "provider": "voters", "model": "m"},
        ],
    }
    cid = (await client.post("/api/v1/councils", headers=auth, json=council)).json()["id"]
    rid = (await client.post(API, headers=auth, json={"topic": "t", "council_id": cid})).json()["id"]
    run = await wait_status(client, auth, rid)
    assert run["status"] == "failed"
    await asyncio.sleep(0.9)
    assert finished_votes == []
    evs = await events_of(client, auth, rid)
    assert evs[-1]["type"] == "run.status" and evs[-1]["data"]["status"] == "failed"


# ----------------------------------------------------------------------------- M10
async def test_start_failure_marks_the_run_failed(client, auth, app):
    def broken_factory(run):
        raise RuntimeError("tool registry exploded")

    app.state.runs.tool_factory = broken_factory
    r = await client.post(API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "run_start_failed"
    rid = r.json()["error"]["details"]["run_id"]
    run = (await client.get(f"{API}/{rid}", headers=auth)).json()
    assert run["status"] == "failed" and "RuntimeError" in run["error"]
    evs = await events_of(client, auth, rid)
    assert types(evs) == ["run.status"] and evs[0]["data"]["status"] == "failed"


async def test_hung_connection_attach_is_bounded(client, auth, app, monkeypatch):
    import argus.runtime.manager as manager_mod

    async def hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(manager_mod, "attach_connections", hang)
    app.state.runs.settings.run_start_timeout_s = 0.2
    r = await asyncio.wait_for(
        client.post(API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2}), 5
    )
    assert r.status_code == 500 and r.json()["error"]["code"] == "run_start_failed"
    run = (await client.get(f"{API}/{r.json()['error']['details']['run_id']}", headers=auth)).json()
    assert run["status"] == "failed"


# ----------------------------------------------------------------------------- L1
async def test_pause_cannot_overwrite_a_terminal_status(client, auth, app):
    gate = asyncio.Event()

    class Gated(MockProvider):
        async def complete(self, request):
            await gate.wait()
            return self._build(request, council_responder(request))

    app.state.llm.system["gatedl1"] = Gated("gatedl1")
    rid = (
        await client.post(
            API,
            headers=auth,
            json={"topic": "t", "mode": "chat", "size": 2, "provider": "gatedl1", "model": "m"},
        )
    ).json()["id"]
    await _wait_for_event(client, auth, rid, "thought.started")
    # Simulate the window between the terminal status write and the run leaving `live`.
    async with app.state.db.sessionmaker() as s:
        await s.execute(update(Run).where(Run.id == uuid.UUID(rid)).values(status="completed"))
        await s.commit()
    assert (await client.post(f"{API}/{rid}/pause", headers=auth)).status_code == 409
    assert (await client.get(f"{API}/{rid}", headers=auth)).json()["status"] == "completed"
    assert (await client.post(f"{API}/{rid}/resume", headers=auth)).status_code == 409
    gate.set()


async def test_cancel_racing_start_is_honoured(client, auth, app, monkeypatch):
    import argus.runtime.manager as manager_mod

    real = manager_mod.skills_for_owner
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_skills(*a, **kw):
        entered.set()
        await release.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(manager_mod, "skills_for_owner", slow_skills)
    create = asyncio.create_task(
        client.post(API, headers=auth, json={"topic": "t", "mode": "chat", "size": 2})
    )
    await asyncio.wait_for(entered.wait(), 5)
    async with app.state.db.sessionmaker() as s:
        rid = (await s.execute(select(Run.id).order_by(Run.created_at.desc()).limit(1))).scalar_one()
    assert (await client.post(f"{API}/{rid}/cancel", headers=auth)).status_code == 202
    release.set()
    await create
    run = await wait_status(client, auth, rid)
    assert run["status"] == "cancelled"
    await _until_not_live(app.state.runs, rid)
    evs = await events_of(client, auth, rid)
    assert evs[-1]["type"] == "run.status" and evs[-1]["data"]["status"] == "cancelled"
    assert not any(e["type"] == "thought.started" for e in evs)
