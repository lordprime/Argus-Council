"""Council members talk to each other, and to the user, as A2A messages and tasks."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from argus.db.models import LedgerEntry
from argus.providers.cli.provider import CLIProvider
from argus.providers.types import Message, ToolCall
from argus.runtime.agent import MemberSpec
from argus.runtime.context import RunCancelled
from argus.runtime.council_bus import MAX_DEPTH, _depth, text_of
from argus.runtime.orchestrator import CouncilOrchestrator
from argus.runtime.resume import TASK_EVENT, ResumeState
from argus.services import ledger
from argus.tools.base import ToolRegistry
from argus.tools.builtin import builtin_tools
from tests import test_limit_extension as limits
from tests.test_cli_adapters import USER, FakeBackend, claude_turn, request
from tests.test_limit_extension import QUIET, FakeLLM, make_ctx, reply
from tests.test_runs import API, _tool_council, _tool_run_setup, _wait_for_event, events_of, wait_status

db, run_row = limits.db, limits.run_row  # the same fixtures


def _member(mid: str, name: str, role: str, tools: list[str] | None = None) -> MemberSpec:
    return MemberSpec(
        id=mid,
        name=name,
        role=role,
        provider="fake",
        model="m",
        system_prompt=f"You are the {role}.",
        tools=tools or [],
    )


def _council(db, run_row, script, rails=None):
    llm = FakeLLM(script)
    ctx, events = make_ctx(db, run_row, rails or QUIET, llm)
    ada, ben, cy = (
        _member("m1", "Ada", "researcher"),
        _member("m2", "Ben", "analyst"),
        _member("m3", "Cy", "writer", ["notes_read"]),
    )
    orch = CouncilOrchestrator(ctx, [ada, ben, cy], ToolRegistry(builtin_tools()))
    return orch, ctx, events, llm, (ada, ben, cy)


def _a2a(events):
    return [e.data for e in events if e.type == "a2a.message"]


async def test_each_one_way_form_reaches_exactly_the_right_members(db, run_row):
    orch, ctx, events, _, (ada, ben, cy) = _council(db, run_row, lambda req: reply("ok"))
    bus = orch.council
    await bus.send(ada, [ben], "Check the 2024 figures", "direct")
    found, _ = bus.resolve(["analyst", "Writer"], ada)
    await bus.send(ada, found, "Draft starts at noon", "group")
    everyone, _ = bus.resolve(["council"], ada)
    await bus.send(ada, everyone, "Scope changed to EU only", "broadcast")
    bus.subscribe(cy, ["risks"])
    _, n = await bus.publish(ada, "risks", "Supplier concentration risk")
    await bus.post_board(ada, "Key source", "IEA 2025 report, table 3", source="https://iea.org/r")

    assert n == 1
    assert [text_of(m) for m in bus.take_inbox(ben.id)] == [
        "Check the 2024 figures",
        "Draft starts at noon",
        "Scope changed to EU only",
    ]
    assert [text_of(m) for m in bus.take_inbox(cy.id)] == [
        "Draft starts at noon",
        "Scope changed to EU only",
        "Supplier concentration risk",
    ]
    assert bus.take_inbox(ada.id) == []  # never your own messages
    forms = [d["message"]["metadata"]["form"] for d in _a2a(events)]
    assert forms == ["direct", "group", "broadcast", "topic", "board"]
    first = _a2a(events)[0]["message"]
    assert first["kind"] == "message" and first["role"] == "agent" and first["contextId"] == str(ctx.run_id)
    assert first["metadata"]["from"]["name"] == "Ada" and first["metadata"]["to"][0]["name"] == "Ben"
    # Each one is in the tamper-evident ledger as A2A, inside the council.
    async with db.sessionmaker() as s:
        rows = list((await s.execute(select(LedgerEntry).where(LedgerEntry.run_id == ctx.run_id))).scalars())
    assert (
        len(rows) == 5
        and {r.protocol for r in rows} == {"a2a"}
        and {r.direction for r in rows} == {"internal"}
    )
    assert (await ledger.verify(db.sessionmaker, ctx.owner_id))["ok"]


async def test_asking_delegating_and_reviewing_run_the_recipient_and_bring_back_its_answer(db, run_row):
    def script(req):
        phase = req.metadata.get("phase")
        return reply(
            {
                "a2a_request": "Ben: the 2024 figure is 41 GW",
                "a2a_delegate": "Cy: summary table attached",
                "a2a_review": "VERDICT: revise\n- cite the IEA table for the 41 GW figure",
            }.get(phase, "ok")
        )

    orch, ctx, events, llm, (ada, ben, cy) = _council(db, run_row, script)
    bus = orch.council
    ok, answer = await bus.request(ada, ben, "What was 2024 capacity?", "request")
    assert ok and answer == "Ben: the 2024 figure is 41 GW"
    asked = llm.requests[-1]
    assert "Ada (researcher) asks you" in asked.messages[1].content
    assert "## Your council (A2A agents)" in asked.messages[0].content

    ok, result = await bus.request(ada, cy, "Tabulate the figures", "delegate", expected="a table")
    assert ok and result == "Cy: summary table attached"
    states = [e.data["state"] for e in events if e.type == "a2a.task"]
    assert states == ["working", "completed"]
    task = next(iter(bus.tasks.values()))
    assert task["status"]["state"] == "completed" and task["artifacts"][0]["parts"][0]["text"] == result

    ok, critique = await bus.request(ada, ben, "Capacity was 41 GW.", "review")
    assert ok and critique.startswith("VERDICT: revise")
    assert ctx.notes[-1]["title"] == "Review of Ada's work: revise"
    replies = [d["message"] for d in _a2a(events) if d["message"]["metadata"].get("inReplyTo")]
    assert [r["metadata"]["from"]["name"] for r in replies] == ["Ben", "Cy", "Ben"]
    assert replies[1]["taskId"] == task["id"]


async def test_exchanges_cannot_nest_without_limit(db, run_row):
    orch, _, _, llm, (ada, ben, _) = _council(db, run_row, lambda req: reply("ok"))
    token = _depth.set(MAX_DEPTH)
    try:
        ok, why = await orch.council.request(ada, ben, "and you?", "request")
    finally:
        _depth.reset(token)
    assert not ok and "deep" in why and llm.requests == []


async def test_teammates_messages_and_the_board_reach_the_next_turn_whatever_the_seat_s_tools(db, run_row):
    orch, _, _, llm, (ada, _, cy) = _council(db, run_row, lambda req: reply("done"))
    bus = orch.council
    await bus.post_board(ada, "Step done: Gather evidence", "Three sources agree on 41 GW")
    await bus.send(ada, [cy], "Lead with the 41 GW figure", "direct")
    await orch.runner(cy).run_task("Write the summary", step_title="Draft")
    req = llm.requests[-1]
    system, turns = req.messages[0].content, [m.content for m in req.messages[1:]]
    assert "## Council board" in system and "Three sources agree on 41 GW" in system
    assert any("[A2A direct message" in t and "Lead with the 41 GW figure" in t for t in turns)
    # Cy's seat grants only notes_read, but every seat can talk to the council.
    names = {t.name for t in req.tools}
    assert {"ask_agent", "delegate_task", "request_review", "broadcast", "reply_to_user"} <= names
    assert "notes_read" in names and "artifact_write" not in names


async def test_the_users_messages_are_a2a_and_stay_in_every_turn(db, run_row):
    orch, _, events, _, (ada, ben, _) = _council(db, run_row, lambda req: reply("ok"))
    bus = orch.council
    to_all = await bus.user_message("Use only sources from 2025 or later", None)
    only_ben = await bus.user_message("Ben: double-check the units", ben)
    assert to_all["role"] == "user" and to_all["metadata"]["from"]["kind"] == "user"
    assert "Use only sources from 2025" in bus.prompt_for(
        ada
    ) and "double-check the units" not in bus.prompt_for(ada)
    assert to_all["messageId"] in bus.prompt_for(ben) and only_ben["messageId"] in bus.prompt_for(ben)
    await bus.reply_to_user(ben, "Units fixed: GW throughout", only_ben["messageId"], None)
    assert only_ben["metadata"]["acknowledged_by"] == ["Ben"]
    directions = [(d["message"]["role"], d["direction"]) for d in _a2a(events)]
    assert directions == [("user", "inbound"), ("user", "inbound"), ("agent", "outbound")]


async def test_a_live_council_takes_a2a_messages_from_the_user(client, auth, app):
    _tool_run_setup(app, "a2alive")
    cid = await _tool_council(client, auth, "a2alive", {"oversight": {"monitor_sensitivity": "off"}})
    rid = (await client.post(API, headers=auth, json={"topic": "ship it", "council_id": cid})).json()["id"]
    await _wait_for_event(client, auth, rid, "approval.requested")  # live, waiting
    body = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "Stage first, then prod"}],
            "messageId": "u-1",
        }
    }
    r = await client.post(f"{API}/{rid}/a2a", headers=auth, json=body)
    assert r.status_code == 202, r.text
    msg = r.json()
    assert msg["kind"] == "message" and msg["role"] == "user" and msg["messageId"] == "u-1"
    assert msg["metadata"]["to"] == [{"name": "council", "kind": "council"}]
    bad = {
        "message": {"role": "user", "parts": [{"kind": "text", "text": "x"}], "metadata": {"to": "Nobody"}}
    }
    assert (await client.post(f"{API}/{rid}/a2a", headers=auth, json=bad)).status_code == 404
    evs = await events_of(client, auth, rid)
    user_msg = next(e for e in evs if e["type"] == "user.message")
    assert user_msg["data"]["message_id"] == "u-1" and user_msg["data"]["text"] == "Stage first, then prod"
    a2a = next(e for e in evs if e["type"] == "a2a.message" and e["data"]["message"]["role"] == "user")
    assert a2a["data"]["direction"] == "inbound" and a2a["data"]["ledger_id"]
    await client.post(f"{API}/{rid}/cancel", headers=auth)
    await wait_status(client, auth, rid)


# ---------------------------------------------------------------- review fixes
async def test_an_answer_task_runs_on_its_own_seat_never_the_members_vendor_session(db, run_row):
    orch, _, _, llm, (ada, ben, _) = _council(db, run_row, lambda req: reply("41 GW"))
    ben.cli_session = str(uuid.uuid4())  # a seat pinned to an existing CLI session
    await orch.council.request(ada, ben, "What was 2024 capacity?", "request")
    meta = llm.requests[-1].metadata
    assert meta["member_id"] == ben.id and meta["cli_seat"].startswith(f"{ben.id}-a2a-")
    assert "cli_session" not in meta  # the pinned session stays the member's own
    await orch.runner(ben).run_task("Gather evidence", step_title="Gather")
    own = llm.requests[-1].metadata
    assert "cli_seat" not in own and own["cli_session"] == ben.cli_session


class _GatedTransport:
    """A CLI runner that holds the next invocation (once armed) until released, to overlap turns."""

    def __init__(self) -> None:
        self.calls = []
        self.armed = False
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def invoke(self, spec):
        self.calls.append(spec)
        self.entered.set()
        if self.armed:
            self.armed = False
            await self.gate.wait()
        for event in claude_turn("ok", spec.session_id, 0.01):
            yield event


async def test_a_cli_seat_and_its_answer_task_never_resume_one_session_at_once():
    transport = _GatedTransport()
    backend = FakeBackend(transport)
    provider = CLIProvider("claude", uuid.UUID(USER), backend)
    step = {"run_id": "run-1", "member_id": "ben"}
    msgs = [Message.system("You are the analyst."), Message.user("Task: analyse")]
    await provider.complete(request(msgs, **step))  # the seat's session exists
    transport.calls.clear()
    transport.armed = True
    transport.entered.clear()
    second = [*msgs, Message.assistant("ok"), Message.user("next")]
    running = asyncio.create_task(provider.complete(request(second, **step)))
    await transport.entered.wait()  # the step's turn is now inside the CLI
    answer = request(
        [Message.system("You are the analyst."), Message.user("Q?")], **step, cli_seat="ben-a2a-1"
    )
    await provider.complete(answer)
    transport.gate.set()
    await running
    own, other = transport.calls
    assert own.resume and own.session_id == provider.seat_session("run-1", "ben").session_id
    assert not other.resume and other.session_id != own.session_id  # a fresh session of its own
    # The step's next turn still sends only its delta: the answer did not disturb its seat.
    third = [*second, Message.assistant("ok"), Message.user("then")]
    await provider.complete(request(third, **step))
    assert transport.calls[-1].resume and "Task: analyse" not in transport.calls[-1].prompt
    assert {r[4] for r in backend.remembered} == {"ben"}  # recorded under the member, not the seat
    provider.forget_run("run-1")
    assert provider.seat_session("run-1", "ben-a2a-1") is None


async def test_a_resumed_run_replays_an_answer_only_to_the_same_question(db, run_row):
    orch, _, events, _, (ada, ben, _) = _council(db, run_row, lambda req: reply("old answer"))
    await orch.council.request(ada, ben, "What was 2024 capacity?", "request")
    done = next(e.data for e in events if e.type == TASK_EVENT)
    resume = ResumeState(from_run_id=uuid.uuid4())
    resume.answers[done["key"]].append("old answer")
    orch2, _, _, llm2, (ada2, ben2, _) = _council(db, run_row, lambda req: reply("fresh answer"))
    orch2.ctx.extra["resume"] = resume
    ok, answer = await orch2.council.request(ada2, ben2, "Which sources disagree?", "request")
    assert ok and answer == "fresh answer" and len(llm2.requests) == 1  # a different question runs
    ok, answer = await orch2.council.request(ada2, ben2, "What was 2024 capacity?", "request")
    assert ok and answer == "old answer" and len(llm2.requests) == 1  # the same one is replayed


async def test_a_run_stopping_during_an_answer_is_not_passed_on_as_an_answer(db, run_row):
    orch, ctx, events, _, (ada, ben, _) = _council(db, run_row, lambda req: reply("ok"))
    ctx.controls.cancelled = True
    with pytest.raises(RunCancelled):
        await orch.council.request(ada, ben, "Still there?", "request")
    assert not [d for d in _a2a(events) if d["message"]["metadata"].get("inReplyTo")]


async def test_an_answer_task_reads_the_members_messages_but_leaves_them_for_its_own_work(db, run_row):
    orch, _, _, llm, (ada, ben, cy) = _council(db, run_row, lambda req: reply("ok"))
    bus = orch.council
    await bus.send(ada, [ben], "Use GW, not MW", "direct")
    await bus.request(cy, ben, "Which unit?", "request")
    assert any("Use GW, not MW" in m.content for m in llm.requests[-1].messages)
    assert [text_of(m) for m in bus.take_inbox(ben.id)] == ["Use GW, not MW"]  # still unread


async def test_members_are_told_only_of_the_ways_to_talk_they_may_use(db, run_row):
    rails = {**QUIET, "tools": {"deny": ["ask_agent"]}}
    calls = iter([[ToolCall(id="c1", name="ask_agent", arguments={"to": "Ben", "question": "x"})], []])
    orch, _, events, llm, (_, _, cy) = _council(db, run_row, lambda req: reply("ok", next(calls)), rails)
    await orch.runner(cy).run_task("Write", step_title="Draft")
    first = llm.requests[0]
    system, names = first.messages[0].content, {t.name for t in first.tools}
    assert "ask_agent" not in names and "- ask_agent:" not in system  # denied by the run's rails
    assert "notes_write" in names and "- notes_write:" in system  # the board belongs to every seat
    assert "artifact_write" not in names  # a seat's own tools still follow its grant
    assert "- send_message:" in system and "send_message" in names
    assert any(e.type == "guardrail.violation" and e.data["rule"] == "tools.deny" for e in events)
    assert llm.requests[1].messages[-1].role == "tool" and "denied" in llm.requests[1].messages[-1].content
    assert orch.council.exchanges == 0  # never reached the bus


def _asks_back(req):
    """Each answer asks the other member back, until a teammate's answer has come in."""
    if req.messages[-1].role == "tool":
        return reply(f"{req.metadata['member']} answers")
    other = "Ada" if req.metadata["member"] == "Ben" else "Ben"
    return reply(
        "", [ToolCall(id=f"c{len(req.messages)}", name="ask_agent", arguments={"to": other, "question": "?"})]
    )


async def test_asking_back_and_forth_stops_at_the_depth_limit(db, run_row):
    orch, _, _, llm, (ada, ben, _) = _council(db, run_row, _asks_back)
    ok, answer = await asyncio.wait_for(orch.council.request(ada, ben, "?", "request"), 10)
    assert ok and answer == "Ben answers"
    assert orch.council.exchanges == 2  # Ada -> Ben -> Ada; Ada's answer may not ask again
    refused = [
        m.content for r in llm.requests for m in r.messages if m.role == "tool" and "deep" in m.content
    ]
    assert refused and len(llm.requests) == 4


async def test_two_members_asking_each_other_at_once_both_get_answers(db, run_row):
    orch, _, _, _, (ada, ben, _) = _council(db, run_row, _asks_back)
    (ok1, a1), (ok2, a2) = await asyncio.wait_for(
        asyncio.gather(
            orch.council.request(ada, ben, "?", "request"), orch.council.request(ben, ada, "?", "request")
        ),
        10,
    )
    assert ok1 and ok2 and a1 == "Ben answers" and a2 == "Ada answers"
