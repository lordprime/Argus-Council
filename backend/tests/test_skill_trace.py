"""Skill trace: the exact text an agent received from a skill is saved as a run artifact and the
skill events carry full detail."""

from __future__ import annotations

from sqlalchemy import select

from argus.db.models import Artifact
from argus.skills.package import SkillPackage
from argus.tools.base import ToolCallContext
from argus.tools.skills import SkillRead, UseSkill
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
