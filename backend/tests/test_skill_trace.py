"""Skill trace: the exact text an agent received from a skill is saved as a run artifact and the
skill events carry full detail."""

from __future__ import annotations

from sqlalchemy import select

from argus.db.models import Artifact, LedgerEntry
from argus.providers.types import ToolCall
from argus.runtime.agent import AgentRunner, MemberSpec
from argus.runtime.oversight import ThoughtBus
from argus.services import ledger
from argus.skills.package import SkillPackage
from argus.tools.base import ToolCallContext, ToolRegistry
from argus.tools.skills import SkillRead, UseSkill, skill_tools
from tests import test_limit_extension as limits
from tests.test_graph_context import QUIET, FakeLLM, _council, _ctx, _owner, reply

db = limits.db  # the same fixture


async def make_ctx(db, rails=None):
    owner = await _owner(db)
    council = await _council(db, owner)
    ctx, events = await _ctx(db, owner, council, FakeLLM(lambda r: reply("x")), rails=rails)
    ctx.skills["brief"] = SkillPackage(
        name="brief", description="Brief things", body="Do X", version="1.0.0", files={"ref.md": "R"}
    )
    return ctx, events


def tctx(ctx, member_id, call_id):
    return ToolCallContext(ctx, member_id, member_id.upper(), "r", None, call_id)


def evs(events, type_):
    return [e for e in events if e.type == type_]


async def artifacts_at(ctx, path):
    async with ctx.sessionmaker() as s:
        rows = await s.execute(select(Artifact).where(Artifact.run_id == ctx.run_id, Artifact.path == path))
        return list(rows.scalars())


async def test_use_skill_saves_instructions_once_and_emits_full_event(db):
    ctx, events = await make_ctx(db)
    out1 = await UseSkill().invoke({"name": "brief"}, tctx(ctx, "m1", "c1"))
    await UseSkill().invoke({"name": "brief"}, tctx(ctx, "m2", "c2"))
    arts = await artifacts_at(ctx, "skills/brief/SKILL.md")
    assert len(arts) == 1 and arts[0].kind == "skill" and arts[0].content == out1.content
    assert arts[0].created_by == "M1"  # the member's name, as the other tools record it
    ev = evs(events, "skill.loaded")
    assert [e.data["member_id"] for e in ev] == ["m1", "m2"]
    assert ev[0].data["artifact_path"] == "skills/brief/SKILL.md"
    assert ev[0].data["files"] == ["ref.md"] and ev[0].data["tool_call_id"] == "c1"
    assert ev[0].data["bytes"] == len(out1.content.encode()) and len(ev[0].data["sha256"]) == 64
    assert ctx.extra["active_skills"] == {"m1": ["brief"], "m2": ["brief"]}


async def test_skill_read_saves_file_and_emits_skill_read(db):
    ctx, events = await make_ctx(db)
    out = await SkillRead().invoke({"name": "brief", "path": "ref.md"}, tctx(ctx, "m1", "c1"))
    assert out.content == "R"
    arts = await artifacts_at(ctx, "skills/brief/ref.md")
    assert len(arts) == 1 and arts[0].content == "R" and arts[0].kind == "skill"
    assert arts[0].created_by == "M1"
    ev = evs(events, "skill.read")
    assert len(ev) == 1
    assert ev[0].data["path"] == "ref.md" and ev[0].data["artifact_path"] == "skills/brief/ref.md"
    assert ev[0].data["name"] == "brief" and ev[0].data["tool_call_id"] == "c1"


async def test_skill_read_missing_path_emits_nothing(db):
    ctx, events = await make_ctx(db)
    out = await SkillRead().invoke({"name": "brief", "path": "nope.md"}, tctx(ctx, "m1", "c1"))
    assert out.is_error
    assert await artifacts_at(ctx, "skills/brief/nope.md") == []
    assert evs(events, "skill.read") == []


async def test_read_only_workspace_still_returns_skill(db):
    rails = {**QUIET, "sandbox": {"read_only_paths": ["skills/*"]}}
    ctx, events = await make_ctx(db, rails)
    out = await UseSkill().invoke({"name": "brief"}, tctx(ctx, "m1", "c1"))
    assert not out.is_error and "Do X" in out.content
    assert evs(events, "skill.loaded")[0].data["artifact_path"] is None
    assert await artifacts_at(ctx, "skills/brief/SKILL.md") == []


async def test_oversize_skill_still_returned_without_artifact(db):
    rails = {**QUIET, "sandbox": {"max_file_mb": 0.0001}}
    ctx, events = await make_ctx(db, rails)
    ctx.skills["brief"].body = "Do X " * 1000
    out = await UseSkill().invoke({"name": "brief"}, tctx(ctx, "m1", "c1"))
    assert not out.is_error and "Do X" in out.content
    assert evs(events, "skill.loaded")[0].data["artifact_path"] is None
    assert await artifacts_at(ctx, "skills/brief/SKILL.md") == []


def _skill_script():
    turn = [0]
    calls = [
        ToolCall(id="c1", name="use_skill", arguments={"name": "brief"}),
        ToolCall(id="c2", name="skill_read", arguments={"name": "brief", "path": "ref.md"}),
        ToolCall(id="c3", name="skill_read", arguments={"name": "brief", "path": "nope.md"}),
    ]

    def script(req):
        turn[0] += 1
        if turn[0] <= len(calls):
            return reply("", [calls[turn[0] - 1]])
        return reply("done")

    return script


async def _run_agent(db):
    ctx, events = await make_ctx(db)
    ctx.llm = FakeLLM(_skill_script())
    member = MemberSpec(
        id="m1", name="Ada", role="researcher", provider="fake", model="m", system_prompt="", tools=["*"]
    )
    runner = AgentRunner(member, ctx, ToolRegistry(skill_tools()), ThoughtBus(ctx, []), "brief")
    out = await runner.run_task("go")
    assert out.ok
    return ctx, events


async def ledger_rows(db, ctx):
    async with db.sessionmaker() as s:
        rows = await s.execute(
            select(LedgerEntry).where(LedgerEntry.owner_id == ctx.owner_id).order_by(LedgerEntry.seq)
        )
        return list(rows.scalars())


async def test_tool_calls_after_use_skill_carry_active_skills(db):
    ctx, events = await _run_agent(db)
    started = [e.data for e in evs(events, "tool.call.started")]
    assert started[0]["active_skills"] == []  # the use_skill call itself
    assert started[1]["active_skills"] == ["brief"]
    assert started[2]["active_skills"] == ["brief"]
    assert started[0]["active_skills"] is not ctx.extra["active_skills"]["m1"]


async def test_skill_calls_write_skill_ledger_rows(db):
    ctx, events = await _run_agent(db)
    rows = await ledger_rows(db, ctx)
    assert [(r.protocol, r.method, r.peer, r.direction, r.status) for r in rows] == [
        ("skill", "use_skill", "brief@1.0.0", "internal", "ok"),
        ("skill", "skill_read", "brief@1.0.0", "internal", "ok"),
        ("skill", "skill_read", "brief@1.0.0", "internal", "error"),
    ]
    assert rows[0].detail == {"member_id": "m1", "path": "skills/brief/SKILL.md"}
    assert (await ledger.verify(db.sessionmaker, ctx.owner_id))["ok"] is True
    assert len(evs(events, "protocol.call")) == 3


async def test_skill_read_error_writes_error_row(db):
    ctx, _ = await make_ctx(db)
    await SkillRead().invoke({"name": "brief", "path": "nope.md"}, tctx(ctx, "m1", "c1"))
    await UseSkill().invoke({"name": "ghost"}, tctx(ctx, "m1", "c2"))
    rows = await ledger_rows(db, ctx)
    assert [(r.method, r.peer, r.status) for r in rows] == [
        ("skill_read", "brief@1.0.0", "error"),
        ("use_skill", "ghost@unknown", "error"),
    ]


async def test_ledger_failure_never_fails_the_skill_call(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("argus.tools.skills.record_call", boom)
    ctx, _ = await make_ctx(db)
    out = await UseSkill().invoke({"name": "brief"}, tctx(ctx, "m1", "c1"))
    assert not out.is_error and "Do X" in out.content
