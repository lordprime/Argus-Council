"""L8: bounded in-process state, compacted agent history and paused time excluded from runtime."""

import asyncio
import gc
import uuid

from argus.providers.mock import MockProvider, council_responder
from argus.providers.types import Message, ToolCall
from argus.runtime.context import Controls
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from argus.runtime.history import compact_history
from argus.services import ledger
from tests.test_runs import wait_status


# ------------------------------------------------------------------ history compaction
def _history(n_tools: int, size: int) -> list[Message]:
    msgs = [Message.system("sys"), Message.user("task")]
    for i in range(n_tools):
        msgs.append(Message.assistant("", [ToolCall(id=f"c{i}", name="artifact_read", arguments={})]))
        msgs.append(Message.tool_result(f"c{i}", f"<out{i}>" + "x" * size))
    return msgs


def _chars(msgs: list[Message]) -> int:
    return sum(len(m.content) for m in msgs)


def test_compaction_shrinks_old_tool_results_and_keeps_recent_ones():
    msgs = _history(10, 20_000)
    saved = compact_history(msgs, budget_chars=60_000, keep_recent=2)
    assert saved > 0 and _chars(msgs) <= 60_000
    tools = [m for m in msgs if m.role == "tool"]
    # The newest results are intact; older ones keep a preview and say what was dropped.
    assert all(len(m.content) > 20_000 for m in tools[-2:])
    assert tools[0].content.startswith("<out0>") and "omitted" in tools[0].content
    # Structure is preserved: every tool result still answers its call.
    assert [m.tool_call_id for m in tools] == [f"c{i}" for i in range(10)]
    assert len(msgs) == 22


def test_compaction_keeps_loaded_skill_instructions():
    msgs = _history(6, 20_000)
    skill = '<tool_output tool="use_skill" untrusted="true">\n# Skill: big\n' + "y" * 60_000
    msgs.insert(2, Message.assistant("", [ToolCall(id="s", name="use_skill", arguments={})]))
    msgs.insert(3, Message.tool_result("s", skill))
    compact_history(msgs, budget_chars=60_000, keep_recent=1)
    assert msgs[3].content == skill  # the oldest result, but never shrunk
    assert "omitted" in msgs[5].content  # ordinary older results still are


def test_compaction_is_a_no_op_under_budget_and_idempotent():
    msgs = _history(3, 100)
    before = [m.content for m in msgs]
    assert compact_history(msgs, budget_chars=60_000) == 0
    assert [m.content for m in msgs] == before
    big = _history(6, 20_000)
    compact_history(big, budget_chars=50_000, keep_recent=1)
    once = [m.content for m in big]
    assert compact_history(big, budget_chars=50_000, keep_recent=1) == 0
    assert [m.content for m in big] == once


async def test_agent_requests_stay_bounded_with_large_tool_outputs(client, auth, app):
    import json

    big = "y" * 40_000
    prompt_sizes: list[int] = []

    def responder(req):
        if req.metadata.get("phase") != "execute":
            return council_responder(req)
        prompt_sizes.append(sum(len(m.content) for m in req.messages))
        turn = sum(1 for m in req.messages if m.role == "tool") // 2
        if turn < 7:
            # Distinct files each turn, so the loop detector does not step in.
            path = f"f{turn}.txt"
            calls = [
                {"name": "artifact_write", "arguments": {"path": path, "content": f"{turn}{big}"}},
                {"name": "artifact_read", "arguments": {"path": path}},
            ]
            return {"content": f"turn {turn}", "tool_calls": calls}
        return {"content": json.dumps({"done": True})}

    app.state.llm.system["hist"] = MockProvider("hist", responder=responder)
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={"topic": "t", "mode": "chat", "size": 2, "provider": "hist", "model": "m"},
    )
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    from argus.runtime import history

    # Without compaction the last request would carry 7 x 40k characters of tool output (280k).
    assert len(prompt_sizes) >= 8
    assert max(prompt_sizes) <= history.HISTORY_BUDGET_CHARS + 2 * len(big)


# ------------------------------------------------------------------ in-process maps
async def test_ledger_locks_are_released_after_use(app):
    owner = uuid.uuid4()
    from argus.db.models import User

    async with app.state.db.sessionmaker() as s:
        s.add(User(id=owner, email=f"{owner.hex}@x.io", password_hash="x"))
        await s.commit()
    await asyncio.gather(
        *(
            ledger.record(
                app.state.db.sessionmaker,
                owner_id=owner,
                protocol="mcp",
                direction="outbound",
                peer="p",
                method="m",
                status="ok",
                request={},
                response={},
            )
            for _ in range(5)
        )
    )
    gc.collect()
    assert not any(key[1] == owner for key in ledger._locks)
    assert (await ledger.verify(app.state.db.sessionmaker, owner))["entries"] == 5


async def test_offline_export_locks_are_released(client, auth, app):
    r = await client.post("/api/v1/runs", headers=auth, json={"topic": "x", "mode": "chat", "size": 2})
    run = await wait_status(client, auth, r.json()["id"])
    await app.state.runs.wait(uuid.UUID(run["id"]))
    r = await client.post(f"/api/v1/runs/{run['id']}/exports", headers=auth, json={"format": "md"})
    assert r.status_code in (200, 201), r.text
    gc.collect()
    assert uuid.UUID(run["id"]) not in app.state.runs._offline_locks


# ------------------------------------------------------------------ paused time
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_paused_time_does_not_count_toward_runtime():
    clock = FakeClock()
    controls = Controls(clock=clock)
    engine = GuardrailEngine(Guardrails.resolve({"max_runtime_minutes": 1}), clock=clock)
    engine.paused_s = controls.paused_s
    clock.t += 30
    controls.resume.clear()  # paused
    clock.t += 600
    assert engine.runtime_s == 30 and engine.check_limits() is None
    controls.resume.set()
    clock.t += 10
    assert engine.runtime_s == 40
    controls.resume.clear()
    controls.resume.clear()  # a second pause while paused changes nothing
    clock.t += 5
    controls.resume.set()
    controls.resume.set()
    assert engine.runtime_s == 40
    clock.t += 25
    assert engine.check_limits() is not None  # 65 s of real work > 1 minute
