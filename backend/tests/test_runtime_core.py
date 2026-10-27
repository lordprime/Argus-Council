import uuid

import pytest

from argus.db.models import Run, User
from argus.db.session import Database
from argus.providers.types import ToolCall
from argus.runtime.context import Workspace
from argus.runtime.events import Actor, EventSink, MemoryBroker, load_events
from argus.runtime.guardrails import GuardrailBreach, GuardrailEngine, Guardrails
from argus.runtime.oversight import LoopDetector, Thought, parse_json_object


@pytest.fixture
async def db(settings):
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def run_row(db):
    async with db.sessionmaker() as s:
        u = User(email=f"{uuid.uuid4().hex}@x.io", password_hash="x")
        s.add(u)
        await s.flush()
        r = Run(owner_id=u.id, topic="t")
        s.add(r)
        await s.commit()
        return r


async def test_sink_seq_persistence_and_fanout(db, run_row):
    broker = MemoryBroker()
    sink = EventSink(run_row.id, None, db.sessionmaker, broker)
    received = []

    async def consume():
        async for payload in broker.subscribe(str(run_row.id)):
            received.append(payload)
            if len(received) == 3:
                break

    import asyncio

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    a = await sink.emit("run.status", {"status": "running"})
    d = await sink.emit(
        "thought.delta", {"thought_id": "t1", "text": "hi"}, actor=Actor(kind="agent", name="A")
    )
    b = await sink.emit("thought.completed", {"thought_id": "t1", "text": "hi"})
    await asyncio.wait_for(task, 1)
    assert (a.seq, d.seq, b.seq) == (1, 1, 2)
    assert d.ephemeral and not b.ephemeral
    stored = await load_events(db.sessionmaker, run_row.id)
    assert [e["type"] for e in stored] == ["run.status", "thought.completed"]
    assert [e["seq"] for e in await load_events(db.sessionmaker, run_row.id, after_seq=1)] == [2]
    async with db.sessionmaker() as s:
        assert (await s.get(Run, run_row.id)).last_seq == 2


async def test_workspace_versions_and_guardrails(db, run_row):
    sink = EventSink(run_row.id, None, db.sessionmaker, MemoryBroker())
    seen = []
    sink.listeners.append(seen.append)
    eng = GuardrailEngine(
        Guardrails.resolve({"sandbox": {"max_file_mb": 0.001, "read_only_paths": ["ro/*"]}})
    )
    ws = Workspace(run_row.id, db.sessionmaker, sink, eng)
    a1 = await ws.write("notes/a.md", "one\ntwo", by="Writer")
    a2 = await ws.write("/workspace/notes/a.md", "one\nthree\nfour", by="Writer")
    assert (a1.version, a2.version) == (1, 2)
    assert seen[-1].data["diff_stats"] == {"added": 2, "removed": 1}
    assert (await ws.read("notes/a.md")).content == "one\nthree\nfour"
    assert (await ws.read("notes/a.md", 1)).content == "one\ntwo"
    assert await ws.read("missing.md") is None
    assert [f["path"] for f in await ws.list()] == ["notes/a.md"]
    with pytest.raises(GuardrailBreach):
        await ws.write("big.txt", "x" * 5000, by="W")
    with pytest.raises(GuardrailBreach):
        await ws.write("ro/x", "y", by="W")
    with pytest.raises(GuardrailBreach):
        await ws.write("../escape", "y", by="W")


def _thought(text="", calls=None, member="m1"):
    return Thought(
        "t",
        Actor(kind="agent", member_id=member, name="A", role="coder"),
        text,
        calls or [],
        None,
        "s",
        False,
    )


def test_loop_detector():
    d = LoopDetector(threshold=3)
    call = [ToolCall(id="1", name="web_fetch", arguments={"url": "x"})]
    assert d.check(_thought(calls=call)) is None
    assert d.check(_thought(calls=call)) is None
    v = d.check(_thought(calls=call))
    assert v.action == "block" and v.reason == "loop" and v.deterministic
    # other members are tracked separately
    assert d.check(_thought(calls=call, member="m2")) is None
    # whitespace/punctuation-only changes still count as repeats
    d2 = LoopDetector(threshold=2)
    d2.check(_thought("Hello, world!"))
    assert d2.check(_thought("hello world")) is not None


def test_parse_json_object():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"action": "pass", "n": {"x": 2}} done') == {
        "action": "pass",
        "n": {"x": 2},
    }
    assert parse_json_object("no json here") is None
    assert parse_json_object("[1, 2]") is None
