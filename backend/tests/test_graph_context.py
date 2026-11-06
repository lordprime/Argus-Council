"""The council knowledge graph: built from a run's events, searched by the graph tools, used for
compact context (digests instead of raw upstream text) and recalled by later runs of the council."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from argus.db.models import Council, GraphEdge, GraphNode, Run, User
from argus.providers.anthropic import AnthropicProvider
from argus.providers.types import CompletionRequest, Message, SystemPrompt, ToolSpec
from argus.runtime.agent import MemberSpec
from argus.runtime.context import RunContext
from argus.runtime.events import EPHEMERAL_TYPES, EventSink, MemoryBroker
from argus.runtime.graph import (
    SUMMARY_CHARS,
    CouncilGraph,
    entities,
    evidence_key,
    graph_for,
    summarize,
)
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from argus.runtime.orchestrator import CouncilOrchestrator, Step
from argus.tools.base import ToolCallContext, ToolRegistry
from argus.tools.builtin import builtin_tools
from argus.tools.graph import GraphLink, GraphQuery, GraphRead
from tests import test_limit_extension as limits
from tests.test_limit_extension import QUIET, FakeLLM, reply

db = limits.db  # the same fixture

URL = "https://www.iea.org/reports/renewables-2025"
PARAGRAPH = (
    "The International Energy Agency reports that Germany added 41 GW of solar capacity in 2024 "
    "[Source 1]. Rooftop systems made up most of it, while utility-scale auctions lagged behind the "
    "targets of the Renewable Energy Sources Act. Grid congestion in Bavaria delayed connections. "
)
LONG_OUTPUT = "\n".join(
    [
        "# Evidence on German solar capacity",
        "## Capacity additions",
        *[f"- Finding {i}: {PARAGRAPH}" for i in range(6)],
        "## Grid constraints",
        PARAGRAPH * 20,
        "## Open questions",
        "- Whether curtailment in Bavaria grows after 2026.",
    ]
)


def _member(mid: str, name: str, role: str) -> MemberSpec:
    return MemberSpec(
        id=mid, name=name, role=role, provider="fake", model="m", system_prompt=f"You are the {role}."
    )


async def _owner(db) -> uuid.UUID:
    async with db.sessionmaker() as s:
        u = User(email=f"{uuid.uuid4().hex}@x.io", password_hash="x")
        s.add(u)
        await s.commit()
        return u.id


async def _council(db, owner: uuid.UUID) -> uuid.UUID:
    async with db.sessionmaker() as s:
        c = Council(owner_id=owner, name=f"c-{uuid.uuid4().hex[:6]}")
        s.add(c)
        await s.commit()
        return c.id


async def _ctx(db, owner, council, llm, *, topic="German solar capacity", rails=None):
    async with db.sessionmaker() as s:
        r = Run(owner_id=owner, council_id=council, topic=topic)
        s.add(r)
        await s.commit()
        run_id = r.id
    sink = EventSink(run_id, council, db.sessionmaker, MemoryBroker())
    events: list = []
    sink.listeners.append(events.append)
    ctx = RunContext(
        run_id=run_id,
        owner_id=owner,
        topic=topic,
        description="How much solar did Germany add, and what limits it?",
        mode="research",
        outputs=["markdown"],
        sink=sink,
        engine=GuardrailEngine(Guardrails.resolve(rails or QUIET)),
        llm=llm,
        sessionmaker=db.sessionmaker,
        approval_timeout_s=5,
    )
    ctx.extra["closers"] = []
    return ctx, events


def _script(req):
    task = next(m.content for m in req.messages if m.role == "user")
    if "Gather evidence" in task.split("Your task:")[-1]:
        return reply(LONG_OUTPUT)
    return reply("## Analysis\nCapacity grew fast; the grid is the constraint.")


async def _two_step_run(db, rails=None, owner=None, council=None):
    owner = owner or await _owner(db)
    llm = FakeLLM(_script)
    ctx, events = await _ctx(db, owner, council, llm, rails=rails)
    ada, ben = _member("m1", "Ada", "researcher"), _member("m2", "Ben", "analyst")
    orch = CouncilOrchestrator(ctx, [ada, ben], ToolRegistry(builtin_tools()))
    orch.steps = [
        Step("s1", "Gather evidence", "Gather evidence", ada),
        Step("s2", "Analyse findings", "Analyse findings", ben, ["s1"]),
    ]
    # The graph exists from the plan on (its first prompt); Ada cites a source while she works on s1.
    graph_for(ctx)
    await ctx.sink.emit(
        "citation.added",
        {
            "source_id": "src_1",
            "n": 1,
            "url": URL,
            "title": "Renewables 2025",
            "snippet": "Germany added 41 GW.",
        },
        step_id="s1",
    )
    await orch.execute()
    return orch, ctx, events, llm


def _task_message(llm, step_title: str) -> str:
    for req in llm.requests:
        first = next(m.content for m in req.messages if m.role == "user")
        if first.rstrip().endswith(step_title):
            return first
    raise AssertionError(f"no request for {step_title}")


# ---------------------------------------------------------------- text helpers
def test_summaries_and_entities_are_deterministic():
    s = summarize(LONG_OUTPUT)
    assert len(s) <= SUMMARY_CHARS and s == summarize(LONG_OUTPUT)
    assert "#" not in s
    found = entities(
        "The International Energy Agency says GPT-4 and the EU AI Act matter. However, Germany leads."
    )
    assert "International Energy Agency" in found and "GPT-4" in found and "Germany" in found
    assert not {"The", "However"} & set(found)
    assert "41GW" not in entities("Capacity reached 41GW in 2024.")


def test_graph_updated_is_ephemeral():
    assert "graph.updated" in EPHEMERAL_TYPES


# ---------------------------------------------------------------- population + digest
async def test_a_run_builds_the_graph_and_dependants_get_a_digest_not_the_raw_text(db):
    orch, ctx, events, llm = await _two_step_run(db)
    graph: CouncilGraph = ctx.extra["graph"]
    await graph.flush()
    s1, s2 = graph.result_key("s1"), graph.result_key("s2")

    # Results, evidence and entities, with their edges.
    assert graph.nodes[s1].type == "result" and graph.nodes[s1].body == LONG_OUTPUT
    assert graph.nodes[s1].author == "Ada"
    evd = evidence_key(URL)
    assert graph.nodes[evd].type == "evidence" and graph.nodes[evd].label.startswith("[Source 1]")
    relations = {(e.src, e.relation, e.dst) for e in graph.edges}
    assert (s1, "cites", evd) in relations
    assert (s2, "derived_from", s1) in relations
    mentioned = {graph.nodes[e.dst].label for e in graph.edges if e.src == s1 and e.relation == "mentions"}
    assert "German" in " ".join(mentioned) or "Grid" in " ".join(mentioned)
    # The step-done board notes are nodes too, tied to the step's result.
    note = next(n for n in graph.nodes.values() if n.key.startswith("nte_") and "Gather" in n.label)
    assert (note.key, "derived_from", s1) in relations

    # Written through to the database, scoped to the run's owner.
    async with db.sessionmaker() as s:
        rows = list((await s.execute(select(GraphNode).where(GraphNode.run_id == ctx.run_id))).scalars())
        edges = list((await s.execute(select(GraphEdge).where(GraphEdge.run_id == ctx.run_id))).scalars())
    assert {r.key for r in rows} >= {s1, s2, evd}
    assert all(r.owner_id == ctx.owner_id and r.council_id is None for r in rows)
    assert len(edges) == len(graph.edges)
    assert any(e.type == "graph.updated" and e.ephemeral for e in events)

    # Ben's step got a digest of s1: summary, key points and the id to read the rest.
    context = _task_message(llm, "Analyse findings")
    assert f"[{s1}]" in context and "Summary:" in context and "Key points:" in context
    assert f'graph_read("{s1}")' in context and "graph_read" in context.split("Your task:")[0]
    assert LONG_OUTPUT not in context
    upstream = context.split("Your task:")[0]
    assert len(upstream) <= orch.ctx.engine.rails.graph.digest_chars + 400

    saved = [e.data for e in events if e.type == "context.saved"]
    ben_saved = next(d for d in saved if d["member_id"] == "m2")
    assert ben_saved["raw_chars"] > 3 * ben_saved["sent_chars"]
    print(
        f"\ncontext.saved for Analyse findings: raw {ben_saved['raw_chars']} chars -> sent "
        f"{ben_saved['sent_chars']} chars ({100 * (1 - ben_saved['sent_chars'] / ben_saved['raw_chars']):.0f}% less)"
    )

    # The graph tools joined the registry, and every seat may call them.
    names = {t.name for t in llm.requests[-1].tools}
    assert {"graph_query", "graph_read", "graph_link"} <= names


async def test_debate_events_become_claims_and_challenges_on_the_board(db):
    orch, ctx, _, _ = await _two_step_run(db)
    graph = ctx.extra["graph"]
    s1 = graph.result_key("s1")
    await ctx.sink.emit(
        "debate.claim",
        {
            "claim_id": "clm_solar41",
            "author_id": "m1",
            "statement": "Germany added 41 GW of solar capacity in 2024",
            "evidence": [{"source": "[Source 1]", "quote": "41 GW"}],
            "confidence": 0.8,
            "status": "open",
        },
        step_id="s1",
    )
    await ctx.sink.emit(
        "debate.challenge",
        {
            "challenge_id": "chl_1",
            "claim_id": "clm_solar41",
            "challenger_id": "m2",
            "target_id": "m1",
            "objection": "Is that AC or DC capacity?",
            "demand": "clarification",
            "round": 0,
        },
    )
    claim = graph.nodes["clm_solar41"]
    assert claim.type == "claim" and claim.author == "Ada" and claim.confidence == 0.8
    relations = {(e.src, e.relation, e.dst) for e in graph.edges}
    assert ("clm_solar41", "cites", evidence_key(URL)) in relations
    assert ("clm_solar41", "derived_from", s1) in relations
    assert ("chl_1", "about", "clm_solar41") in relations
    assert any(graph.nodes[e.dst].label == "Germany" for e in graph.edges if e.src == "clm_solar41")

    board = orch.council.board_text()
    assert "Open challenges:" in board and "[chl_1] Ben → Ada on clm_solar41 (clarification)" in board
    # The dependency digest lists the step's claims as one-liners with ids.
    digest = graph.dependency_digest(orch.steps[0])
    assert "Claims:" in digest and "[clm_solar41] (open) Germany added 41 GW" in digest

    await ctx.sink.emit(
        "debate.response",
        {
            "challenge_id": "chl_1",
            "responder_id": "m1",
            "stance": "defend",
            "argument": "AC.",
            "evidence": [],
        },
    )
    await ctx.sink.emit(
        "debate.resolution", {"challenge_id": "chl_1", "outcome": "rejected", "note": "unclear"}
    )
    await ctx.sink.emit("debate.claim_status", {"claim_id": "clm_solar41", "status": "disputed"})
    assert graph.nodes["chl_1"].status == "closed:rejected" and "AC." in graph.nodes["chl_1"].body
    assert ("chl_1", "refutes", "clm_solar41") in {(e.src, e.relation, e.dst) for e in graph.edges}
    assert graph.nodes["clm_solar41"].status == "disputed"
    assert "Open challenges" not in orch.council.board_text()
    await graph.flush()
    async with db.sessionmaker() as s:
        row = (await s.execute(select(GraphNode).where(GraphNode.key == "clm_solar41"))).scalar_one()
    assert row.status == "disputed" and row.type == "claim"


async def test_board_shows_summaries_with_ids_for_long_notes(db):
    orch, ctx, _, _ = await _two_step_run(db)
    ada = orch.members[0]
    await orch.council.post_board(ada, "Raw dump", "x " * 2_000)
    board = orch.council.board_text()
    raw = orch.council.board_text_raw()
    assert len(board) < len(raw)
    line = next(line for line in board.splitlines() if "Raw dump" in line)
    graph = ctx.extra["graph"]
    note_key = next(n.key for n in graph.nodes.values() if n.label == "Raw dump")
    assert line.endswith(f"[{note_key}]") and len(line) < 400


# ---------------------------------------------------------------- retrieval + tools
async def test_query_ranks_by_bm25_and_expands_along_edges(db):
    _, ctx, _, _ = await _two_step_run(db)
    graph = ctx.extra["graph"]
    s1, s2 = graph.result_key("s1"), graph.result_key("s2")
    hits = graph.search("grid constraints Bavaria", limit=3, hops=0)
    assert hits and hits[0][1].key in {s1} | {n.key for n in graph.nodes.values() if n.type == "entity"}
    assert all(h == 0 for _, _, h in hits)
    # Types filter, then one hop out from the evidence reaches the results that cite it.
    only = graph.search("Renewables 2025", types=["evidence"], hops=0, limit=5)
    assert [n.key for _, n, _ in only] == [evidence_key(URL)]
    expanded = graph.search("Renewables 2025", types=["evidence", "result"], hops=1, limit=3)
    assert expanded[0][1].key == evidence_key(URL)
    assert any(n.key == s1 and h == 1 for _, n, h in expanded)
    assert s2 not in {n.key for _, n, h in expanded if h == 0}

    tctx = ToolCallContext(ctx, "m2", "Ben", "analyst", "s2", "call_1")
    out = await GraphQuery().invoke({"query": "solar capacity Germany", "limit": 3}, tctx)
    assert not out.is_error and f"[{s1}]" in out.content and "graph_read" in out.content
    bad = await GraphQuery().invoke({"query": "x", "types": ["nope"]}, tctx)
    assert bad.is_error and "claim" in bad.content
    full = await GraphRead().invoke({"node_id": s1}, tctx)
    assert not full.is_error and LONG_OUTPUT[:200] in full.content and "Edges:" in full.content
    assert "cites →" in full.content
    missing = await GraphRead().invoke({"node_id": "res_nope"}, tctx)
    assert missing.is_error and "graph_query" in missing.content
    linked = await GraphLink().invoke({"from": s2, "to": evidence_key(URL), "relation": "cites"}, tctx)
    assert not linked.is_error and linked.data["added"]
    again = await GraphLink().invoke({"from": s2, "to": evidence_key(URL), "relation": "cites"}, tctx)
    assert again.data["added"] is False
    wrong = await GraphLink().invoke({"from": s2, "to": s1, "relation": "loves"}, tctx)
    assert wrong.is_error
    assert all(t.parallel_safe and t.source == "council" for t in (GraphQuery(), GraphRead(), GraphLink()))


# ---------------------------------------------------------------- council recall
async def test_a_later_run_of_the_council_recalls_what_earlier_runs_learned(db):
    owner = await _owner(db)
    council = await _council(db, owner)
    _, first, _, _ = await _two_step_run(db, owner=owner, council=council)
    await first.extra["graph"].close()

    llm = FakeLLM(lambda req: reply("ok"))
    ctx, _ = await _ctx(db, owner, council, llm, topic="Outlook for German solar capacity")
    graph = graph_for(ctx)
    await graph.ready()
    recall = graph.recall_text()
    assert "What this council already knows" in recall
    assert first.extra["graph"].result_key("s1") in recall and "(earlier run)" in recall
    assert recall == graph.recall_text()  # fixed for the run

    # It is part of every member's stable instructions.
    orch = CouncilOrchestrator(ctx, [_member("m1", "Ada", "researcher")], ToolRegistry(builtin_tools()))
    await orch.runner(orch.members[0]).run_task("Plan", step_title="Plan")
    system = llm.requests[-1].messages[0]
    assert "What this council already knows" in system.content
    assert system.cache_prefix and "What this council already knows" in system.content[: system.cache_prefix]

    # Another council of the same owner, and another owner, see none of it.
    other_council = await _council(db, owner)
    ctx2, _ = await _ctx(db, owner, other_council, llm, topic="Outlook for German solar capacity")
    g2 = graph_for(ctx2)
    await g2.ready()
    assert g2.recall_text() == "" and not any(n.prior for n in g2.nodes.values())
    stranger = await _owner(db)
    ctx3, _ = await _ctx(db, stranger, None, llm, topic="Outlook for German solar capacity")
    g3 = graph_for(ctx3)
    await g3.ready()
    assert g3.recall_text() == "" and not g3.nodes


# ---------------------------------------------------------------- prompt caching
async def test_system_prompt_splits_into_a_stable_prefix_and_a_volatile_tail(db):
    _, _, _, llm = await _two_step_run(db)
    system = llm.requests[-1].messages[0]
    prefix = system.content[: system.cache_prefix]
    assert "Topic: German solar capacity" in prefix and "Guardrails:" in prefix
    assert "## Council board" not in prefix and "## Council board" in system.content[system.cache_prefix :]
    # Providers without caching see the same text as before.
    req = llm.requests[-1]
    assert req.system_prompt == system.content


def test_anthropic_marks_the_stable_prefix_and_the_tools_as_cacheable():
    provider = AnthropicProvider(client=object(), refusal_fallback=False)
    tools = [ToolSpec(name="a"), ToolSpec(name="b")]
    text = "persona and guardrails\n## Council board\n#1 note"
    req = CompletionRequest(
        model="claude-x",
        messages=[Message.system(SystemPrompt(text, len("persona and guardrails"))), Message.user("hi")],
        tools=tools,
    )
    params = provider._params(req)
    assert params["system"] == [
        {"type": "text", "text": "persona and guardrails", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "## Council board\n#1 note"},
    ]
    assert params["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in params["tools"][0]
    assert params["messages"] == [{"role": "user", "content": "hi"}]

    plain = provider._params(
        CompletionRequest(model="claude-x", messages=[Message.system(text)], tools=tools)
    )
    assert plain["system"] == text and all("cache_control" not in t for t in plain["tools"])


# ---------------------------------------------------------------- off switch
async def test_graph_disabled_keeps_the_raw_context_and_adds_nothing(db):
    rails = {**QUIET, "graph": {"enabled": False, "prompt_cache": False}}
    orch, ctx, events, llm = await _two_step_run(db, rails=rails)
    assert "graph" not in ctx.extra
    context = _task_message(llm, "Analyse findings")
    assert context.startswith(
        f"Context from the council so far:\n## Gather evidence (Ada)\n{LONG_OUTPUT}\n\n"
    )
    assert "graph_read" not in context
    assert not any(e.type in ("context.saved", "graph.updated") for e in events)
    assert not {"graph_query", "graph_read", "graph_link"} & {t.name for t in llm.requests[-1].tools}
    assert orch.council.board_text() == orch.council.board_text_raw()
    assert llm.requests[-1].messages[0].cache_prefix is None
    async with db.sessionmaker() as s:
        assert (await s.execute(select(GraphNode).where(GraphNode.run_id == ctx.run_id))).first() is None
    tctx = ToolCallContext(ctx, "m2", "Ben", "analyst", "s2", "call_1")
    off = await GraphQuery().invoke({"query": "x"}, tctx)
    assert off.is_error and "turned off" in off.content


@pytest.mark.parametrize("cached", [True, False])
async def test_prompt_cache_flag_controls_only_the_split(db, cached):
    rails = {**QUIET, "graph": {"prompt_cache": cached}}
    _, _, _, llm = await _two_step_run(db, rails=rails)
    system = llm.requests[-1].messages[0]
    assert bool(system.cache_prefix) is cached
