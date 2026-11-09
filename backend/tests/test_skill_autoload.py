"""Skills an agent's task calls for are loaded for it automatically, so skill use never depends on
the model choosing to call use_skill."""

from __future__ import annotations

from argus.runtime.agent import AgentRunner, MemberSpec
from argus.runtime.oversight import ThoughtBus
from argus.skills.auto import pick_skills
from argus.skills.package import SkillPackage
from argus.tools.base import ToolRegistry
from argus.tools.skills import skill_tools
from tests import test_limit_extension as limits
from tests.test_graph_context import FakeLLM, _council, _ctx, _owner, reply
from tests.test_skill_trace import artifacts_at, evs

db = limits.db  # the same fixture

RESEARCH = SkillPackage(
    name="research-report",
    description=(
        "Plan, evidence and write a rigorous research report with inline citations. Use for any "
        "research-mode deliverable, literature review, market scan or technical investigation."
    ),
    body="Cite every claim.",
    version="1.0.0",
)
SOURCES = SkillPackage(
    name="source-evaluation",
    description=(
        "Score the reliability of a source (currency, authority, accuracy, purpose) before relying on "
        "it. Use whenever research depends on web pages, preprints or claims whose trustworthiness is "
        "unclear."
    ),
    body="Score each source.",
    version="1.0.0",
)
REVIEW = SkillPackage(
    name="code-review",
    description=(
        "Review code for correctness, security and maintainability with a severity rubric. Use when "
        "asked to review, audit or critique code, or before accepting code produced by the council."
    ),
    body="Rate findings by severity.",
    version="1.0.0",
)
LATEX = SkillPackage(
    name="latex-paper",
    description=(
        "Write academic papers, technical reports or theses in LaTeX with BibTeX references. Use when "
        "the requested output format is LaTeX or PDF and the content is academic or technical."
    ),
    body="Use BibTeX.",
    version="1.0.0",
)
ALL = {p.name: p for p in (RESEARCH, SOURCES, REVIEW, LATEX)}


def test_research_runs_get_the_research_skills():
    picked = pick_skills(ALL, mode="research", role="researcher", text="Collect public profile evidence")
    assert set(picked) == {"research-report", "source-evaluation"}


def test_the_task_text_selects_skills_outside_the_mode():
    picked = pick_skills(ALL, mode="coding", role="critic", text="Review this code for security issues")
    assert picked[0] == "code-review"


def test_unrelated_work_loads_nothing():
    assert pick_skills(ALL, mode="chat", role="assistant", text="Say hello to the user") == []


def test_at_most_the_limit():
    assert len(pick_skills(ALL, mode="research", role="writer", text="research report", limit=1)) == 1


async def _runner(db, script=None):
    owner = await _owner(db)
    council = await _council(db, owner)
    llm = FakeLLM(script or (lambda r: reply("done")))
    ctx, events = await _ctx(db, owner, council, llm)  # mode "research"
    ctx.skills.update(ALL)
    member = MemberSpec(
        id="m1", name="Ada", role="researcher", provider="fake", model="m", system_prompt="", tools=["*"]
    )
    return (
        AgentRunner(member, ctx, ToolRegistry(skill_tools()), ThoughtBus(ctx, []), "brief"),
        ctx,
        events,
        llm,
    )


async def test_matching_skills_are_loaded_into_the_task_and_traced(db):
    runner, ctx, events, llm = await _runner(db)
    out = await runner.run_task("Collect public profile and portfolio evidence", step_id="s1")
    assert out.ok
    prompt = "\n".join(m.content for m in llm.requests[0].messages if isinstance(m.content, str))
    assert "Cite every claim." in prompt and "Score each source." in prompt
    loaded = evs(events, "skill.loaded")
    assert {e.data["name"] for e in loaded} == {"research-report", "source-evaluation"}
    assert all(e.data["auto"] is True and e.data["member_id"] == "m1" for e in loaded)
    assert set(ctx.extra["active_skills"]["m1"]) == {"research-report", "source-evaluation"}
    assert await artifacts_at(ctx, "skills/research-report/SKILL.md")


async def test_tool_less_tasks_load_no_skills(db):
    runner, _ctx2, events, _llm = await _runner(db)
    out = await runner.run_task("Cast your vote on the research report", tools="none")
    assert out.ok
    assert evs(events, "skill.loaded") == []


async def test_a_member_without_use_skill_gets_none(db):
    runner, _ctx2, events, _llm = await _runner(db)
    runner.member.tools = ["web_fetch"]
    await runner.run_task("Collect public profile and portfolio evidence")
    assert evs(events, "skill.loaded") == []
