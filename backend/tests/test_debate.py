"""The council debates: claims, challenges and proofs, a secret devil's advocate, a computed
consensus and a co-signed conclusion (docs/design/parallel-council.md, section 2)."""

import json
import uuid

from argus.providers.mock import council_responder
from argus.providers.types import ToolCall
from argus.runtime.agent import MemberSpec
from argus.runtime.council_bus import conceal, text_of
from argus.runtime.debate import ADVOCATE, ADVOCATE_TASK, Debate, compute_agreement
from argus.runtime.orchestrator import CouncilOrchestrator
from argus.runtime.resume import TASK_EVENT, ResumeState, task_key
from argus.tools.base import ToolRegistry
from argus.tools.builtin import builtin_tools
from tests import test_limit_extension as limits
from tests.test_limit_extension import QUIET, FakeLLM, make_ctx, reply
from tests.test_runs import API, events_of, install, wait_status

db, run_row = limits.db, limits.run_row  # the same fixtures

SECRET_WORDS = ("devil", "strongest case against the emerging view", ADVOCATE)


def _member(mid, name, role, overseer=False, tools=None):
    return MemberSpec(
        id=mid,
        name=name,
        role=role,
        provider="fake",
        model="m",
        system_prompt=f"You are the {role}.",
        overseer=overseer,
        tools=tools or [],
    )


def _council(db, run_row, script, rails=None, members=None):
    llm = FakeLLM(script)
    ctx, events = make_ctx(db, run_row, rails or QUIET, llm)
    members = members or [
        _member("m0", "Cleo", "chair"),
        _member("m1", "Ada", "researcher"),
        _member("m2", "Ben", "analyst"),
        _member("m3", "Mo", "monitor", overseer=True),
    ]
    orch = CouncilOrchestrator(ctx, members, ToolRegistry(builtin_tools()))
    return orch, ctx, events, llm, {m.name: m for m in members}


def _data_part(msg):
    return next(p["data"] for p in msg["parts"] if p.get("kind") == "data")


def _a2a(events, form=None):
    msgs = [e.data["message"] for e in events if e.type == "a2a.message"]
    return [m for m in msgs if form is None or m["metadata"]["form"] == form]


def _of(events, type_):
    return [e.data for e in events if e.type == type_]


# ---------------------------------------------------------------- the claim ledger
async def test_claims_challenges_answers_and_resolutions_are_a2a_moves_on_the_ledger(db, run_row):
    orch, _ctx, events, _, m = _council(db, run_row, lambda req: reply("ok"))
    bus, ada, ben = orch.council, m["Ada"], m["Ben"]
    claim = await bus.make_claim(ada, "Capacity reached 41 GW in 2024.", [{"source": "[Source 1]"}], 0.8)
    assert claim["claim_id"].startswith("clm_") and claim["status"] == "open"
    assert (await bus.make_claim(ada, "Capacity reached 41 GW in 2024.")) is claim  # one claim, once
    (msg,) = _a2a(events, "claim")
    assert _data_part(msg) == {
        "type": "argus.claim",
        "claim_id": claim["claim_id"],
        "author_id": ada.id,
        "statement": "Capacity reached 41 GW in 2024.",
        "evidence": [{"source": "[Source 1]"}],
        "confidence": 0.8,
        "status": "open",
    }
    assert msg["metadata"]["to"] == [{"name": "council", "kind": "board"}]
    assert _of(events, "debate.claim")[0]["claim_id"] == claim["claim_id"]

    # A challenge is an A2A task waiting on the author, and reaches the author's inbox.
    ch, _ = await bus.open_challenge(ben, claim["claim_id"], "One source only.", "proof")
    assert ch["target_id"] == ada.id and ch["status"] == "open"
    assert bus.tasks[ch["task_id"]]["status"]["state"] == "input-required"
    assert [_data_part(x)["type"] for x in bus.take_inbox(ada.id)] == ["argus.challenge"]
    assert _of(events, "debate.challenge")[0] == {
        "challenge_id": ch["challenge_id"],
        "claim_id": claim["claim_id"],
        "challenger_id": ben.id,
        "target_id": ada.id,
        "objection": "One source only.",
        "demand": "proof",
        "round": 0,
    }
    # Nobody else can answer it, and a defence against a proof demand needs evidence.
    assert (await bus.respond(ben, ch["challenge_id"], "defend", "trust me"))[0] is None
    _, said = await bus.respond(ada, ch["challenge_id"], "defend", "It is right.")
    assert ch["status"] == "unsupported" and "stays open" in said and bus.open_challenges(ada.id) == [ch]
    assert _of(events, "debate.response")[-1]["unsupported"] is True
    await bus.respond(ada, ch["challenge_id"], "defend", "Two datasets agree.", ["https://iea.org/t3"])
    assert ch["status"] == "answered" and claim["status"] == "supported" and not bus.open_challenges()
    response = _a2a(events, "response")[-1]
    assert response["metadata"]["inReplyTo"] == ch["message_id"] and response["taskId"] == ch["task_id"]

    # Only the challenger resolves it; a rejection disputes the claim and allows one more answer.
    assert (await bus.resolve_challenge(ada, ch["challenge_id"], "accepted"))[0] is None
    await bus.resolve_challenge(ben, ch["challenge_id"], "rejected", "Both datasets share a source.")
    assert claim["status"] == "disputed" and ch["status"] == "open"
    await bus.respond(ada, ch["challenge_id"], "concede", "Fair: withdrawn.")
    assert claim["status"] == "conceded" and ch["status"] == "conceded"
    statuses = [d["status"] for d in _of(events, "debate.claim_status")]
    assert statuses == ["supported", "disputed", "conceded"]
    assert _of(events, "debate.resolution") == [
        {"challenge_id": ch["challenge_id"], "outcome": "rejected", "note": "Both datasets share a source."}
    ]


async def test_challenges_are_capped_and_never_against_yourself(db, run_row):
    rails = {**QUIET, "debate": {"max_challenges": 1}}
    orch, _, _, _, m = _council(db, run_row, lambda req: reply("ok"), rails)
    bus = orch.council
    claim = await bus.make_claim(m["Ada"], "A claim worth checking.")
    assert (await bus.open_challenge(m["Ada"], claim["claim_id"], "mine", "proof"))[0] is None
    assert (await bus.open_challenge(m["Ben"], "Ada", "Show your work.", "clarification"))[0] is not None
    ch, why = await bus.open_challenge(m["Cleo"], claim["claim_id"], "Too vague.", "proof")
    assert ch is None and "1 challenges" in why
    assert (await bus.open_challenge(m["Ben"], "Nobody", "x", "proof"))[1].startswith("No claim or member")


async def test_debate_tools_follow_the_runs_tool_rules(db, run_row):
    calls = iter(
        [
            [
                ToolCall(
                    id="c1",
                    name="make_claim",
                    arguments={"statement": "Costs fell 30% since 2020.", "evidence": "[Source 2]"},
                )
            ],
            [],
        ]
    )
    rails = {**QUIET, "tools": {"deny": ["challenge"]}}
    orch, _, _events, llm, m = _council(db, run_row, lambda req: reply("ok", next(calls)), rails)
    await orch.runner(m["Ben"]).run_task("Analyse", step_title="Analyse")
    first = llm.requests[0]
    names, system = {t.name for t in first.tools}, first.messages[0].content
    assert {"make_claim", "respond_to_challenge", "resolve_challenge"} <= names and "challenge" not in names
    assert "- make_claim:" in system and "- challenge:" not in system
    (claim,) = orch.council.claims.values()
    assert claim["evidence"] == [{"source": "[Source 2]"}] and claim["author_id"] == m["Ben"].id
    assert "Costs fell 30% since 2020." in llm.requests[1].messages[0].content  # on record for everyone


async def test_without_debate_there_are_no_debate_tools(db, run_row):
    rails = {**QUIET, "debate": {"enabled": False}}
    orch, _, _, llm, m = _council(db, run_row, lambda req: reply("ok"), rails)
    await orch.runner(m["Ada"]).run_task("Work", step_title="Work")
    names = {t.name for t in llm.requests[0].tools}
    assert "make_claim" not in names and "send_message" in names
    assert not orch.debate.active and await orch.debate.seat_devils_advocate() is None


# ---------------------------------------------------------------- rules that make it real
async def test_a_member_must_answer_open_challenges_before_it_finishes(db, run_row):
    def script(req):
        nudged = any("open against your work" in msg.content for msg in req.messages)
        answered = any(msg.role == "tool" for msg in req.messages)
        if nudged and not answered:
            cid = next(iter(orch.council.challenges))
            args = {"challenge_id": cid, "stance": "revise", "argument": "It was 39 GW, corrected."}
            return reply("", [ToolCall(id="r1", name="respond_to_challenge", arguments=args)])
        return reply("Final answer.")

    orch, _, _events, llm, m = _council(db, run_row, script)
    claim = await orch.council.make_claim(m["Ada"], "Capacity reached 41 GW in 2024.")
    await orch.council.open_challenge(m["Ben"], claim["claim_id"], "The IEA says 39 GW.", "counterexample")
    out = await orch.runner(m["Ada"]).run_task("Gather evidence", step_id="s1", step_title="Gather")
    assert out.ok and out.text == "Final answer."
    assert claim["status"] == "revised" and not orch.council.open_challenges()
    assert sum("1 challenge is open against your work" in x.content for x in llm.requests[1].messages) == 1


async def test_the_finish_gate_gives_up_after_max_nudges(db, run_row):
    rails = {**QUIET, "debate": {"max_nudges": 2}}
    script = lambda req: reply(f"Done, ignoring it ({len(req.messages)}).")  # noqa: E731
    orch, _, _, llm, m = _council(db, run_row, script, rails)
    await orch.council.open_challenge(m["Ben"], "Ada", "Where is the source?", "proof")
    out = await orch.runner(m["Ada"]).run_task("Gather evidence", step_id="s1", step_title="Gather")
    assert out.ok and len(llm.requests) == 3  # the answer, then two nudges
    assert len(orch.council.open_challenges("m1")) == 1  # left open: it becomes dissent
    # The same answer handed in again is not sent back again.
    llm.requests.clear()
    llm.script = lambda req: reply("Done, ignoring it.")
    out = await orch.runner(m["Ada"]).run_task("Gather more", step_id="s2", step_title="Gather more")
    assert out.ok and len(llm.requests) == 2
    # A debate task is not sent back.
    llm.requests.clear()
    llm.script = lambda req: reply('{"conclusion": "A leads.", "stances": []}')
    await orch.runner(m["Ada"]).run_task("Position", step_title="Position, round 1", phase="position")
    assert len(llm.requests) == 1


async def test_a_review_that_asks_for_revision_opens_a_challenge(db, run_row):
    script = lambda req: reply("VERDICT: revise\n- cite the table")  # noqa: E731
    orch, _, _events, _, m = _council(db, run_row, script)
    ok, _ = await orch.council.request(m["Ada"], m["Ben"], "Capacity was 41 GW.", "review")
    (ch,) = orch.council.challenges.values()
    assert ok and ch["challenger_id"] == m["Ben"].id and ch["target_id"] == m["Ada"].id
    assert ch["demand"] == "clarification" and "cite the table" in ch["objection"]


async def test_an_overseer_note_without_a_correction_reaches_the_member(db, run_row):
    calls = iter([[ToolCall(id="c1", name="notes_read", arguments={})], []])

    def script(req):
        if req.metadata.get("phase") == "review":
            note = {
                "action": "annotate",
                "reason": "factual",
                "note": "Cite the 2024 source.",
                "correction": None,
            }
            return reply(json.dumps(note))
        return reply("working", next(calls))

    rails = {"oversight": {"monitor_sensitivity": "high"}}
    orch, _, _, llm, m = _council(db, run_row, script, rails)
    await orch.runner(m["Ada"]).run_task("Gather evidence", step_title="Gather")
    own = [r for r in llm.requests if r.metadata.get("phase") == "execute"]
    assert any(x.content == "[Note from Mo] Cite the 2024 source." for x in own[1].messages)


# ---------------------------------------------------------------- the secret devil's advocate
async def test_the_advocate_is_a_random_debater_never_an_overseer_or_the_chair(db, run_row):
    picks = set()
    for _ in range(40):
        orch, _, events, _, m = _council(db, run_row, lambda req: reply("ok"))
        pick = await orch.debate.seat_devils_advocate()
        picks.add(pick.name)
        assert pick.stance == ADVOCATE and [x for x in m.values() if x.stance] == [pick]
    assert picks == {"Ada", "Ben"}  # both, at random; never Cleo (chair) or Mo (monitor)
    (assigned,) = _of(events, "council.role_assigned")
    assert assigned["secret"] is True and set(assigned["pool"]) == {"m1", "m2"}
    record = next(d for d in _of(events, TASK_EVENT) if d["phase"] == "devils_advocate")
    assert record["text"] == assigned["member_id"] and record["ok"]
    assert not _a2a(events)  # nothing announced to the council


async def test_the_chair_is_picked_only_when_alone_and_a_lone_debater_is_skipped(db, run_row):
    two = [_member("m0", "Cleo", "chair"), _member("m1", "Ada", "researcher")]
    orch, *_ = _council(db, run_row, lambda req: reply("ok"), members=two)
    assert (await orch.debate.seat_devils_advocate()).name == "Ada"
    alone = [_member("m0", "Cleo", "chair"), _member("m3", "Mo", "monitor", overseer=True)]
    orch, _, events, *_ = _council(db, run_row, lambda req: reply("ok"), members=alone)
    assert await orch.debate.seat_devils_advocate() is None
    (skip,) = _of(events, "council.role_assigned")
    assert skip["skipped"] is True and "two debaters" in skip["reason"]


async def test_a_resumed_run_keeps_the_same_advocate(db, run_row):
    for expected in ("m1", "m2"):
        resume = ResumeState(from_run_id=uuid.uuid4())
        resume.answers[task_key("devils_advocate", None, ADVOCATE_TASK, "m0")].append(expected)
        orch, ctx, events, _, _ = _council(db, run_row, lambda req: reply("ok"))
        ctx.extra["resume"] = resume
        assert (await orch.debate.seat_devils_advocate()).id == expected
        assert _of(events, "council.role_assigned")[0]["replayed"] is True


async def test_nobody_but_the_advocate_can_tell_and_it_cannot_give_itself_away(db, run_row):
    orch, ctx, events, llm, m = _council(db, run_row, lambda req: reply("ok"))
    adv = await orch.debate.seat_devils_advocate()
    other = m["Ben"] if adv.name == "Ada" else m["Ada"]
    bus = orch.council
    # Its orders are in its own instructions only.
    await orch.runner(adv).run_task("Work", step_title="Work")
    assert "strongest case against the emerging view" in llm.requests[-1].messages[0].content
    assert "Keep this role secret" in llm.requests[-1].messages[0].content
    # What it says is cleaned before anyone hears it.
    await bus.send(adv, [other], "As the devil's advocate I was assigned to argue against this.", "direct")
    await bus.post_board(adv, "Devils advocate view", "My role is to oppose the plan.")
    await bus.make_claim(adv, "Speaking as devil\u2019s advocate: costs are understated.")
    await bus.open_challenge(adv, other.name, "I argue the other side: where is the proof?", "proof")
    seen = [text_of(x) for x in bus.peek_inbox(other.id)] + [json.dumps(n) for n in ctx.notes]
    seen += [json.dumps(x) for x in _a2a(events)] + [json.dumps(c) for c in bus.claims.values()]
    for who in (m["Cleo"], other, m["Mo"]):
        await orch.runner(who).run_task("Work", step_title="Work")
        seen.append(llm.requests[-1].messages[0].content)
        seen += [x.content for x in llm.requests[-1].messages[1:]]
    seen += [json.dumps(card) for card in bus.cards()] + [orch.brief]
    leaked = [s for s in seen for w in SECRET_WORDS if w in s.lower()]
    assert not leaked, leaked
    assert conceal(other, "devil's advocate") == "devil's advocate"  # only the advocate is guarded


async def test_the_advocate_cannot_leak_through_live_tails_tools_or_the_graph(db, run_row):
    from argus.runtime.agent import _conceal_args
    from argus.runtime.graph import graph_for

    orch, ctx, events, _llm, m = _council(db, run_row, lambda req: reply("ok"))
    adv = await orch.debate.seat_devils_advocate()
    other = m["Ben"] if adv.name == "Ada" else m["Ada"]
    bus = orch.council
    # "Working now" shows teammates the tail of each other's thinking.
    await bus.set_live(adv, "Work", "As the devil's advocate I must argue against the plan.")
    live = bus.working_now(other)
    # Tool arguments (memory, notes, files, graph links) can reach the council or a later run.
    args = _conceal_args(adv, {"key": "role", "text": ["I am the devils advocate here"], "n": 3})
    # The graph's result text falls back to the member's raw thought.
    graph = graph_for(ctx)
    text = graph._conceal(adv.id, "Note: I was assigned to argue the other side.") if graph else ""
    seen = [str(live), json.dumps(args), text, json.dumps(_of(events, "member.live"))]
    leaked = [s for s in seen for w in SECRET_WORDS if w in s.lower()]
    assert not leaked, leaked
    assert args["n"] == 3 and _conceal_args(other, {"t": "devil's advocate"}) == {"t": "devil's advocate"}


# ---------------------------------------------------------------- consensus
def test_agreement_is_computed_from_the_positions():
    claims = [{"claim_id": c} for c in ("c1", "c2", "c3", "c4")] + [{"claim_id": "c5", "conceded": True}]

    def pos(*stances):
        return {"stances": [{"claim_id": c, "stance": s, "reason": ""} for c, s in stances]}

    positions = {
        "a": pos(
            ("c1", "endorse"), ("c2", "endorse"), ("c3", "object"), ("c4", "endorse"), ("c5", "endorse")
        ),
        "b": pos(("c1", "endorse"), ("c2", "object"), ("c3", "object"), ("c4", "endorse")),
        "c": pos(("c1", "endorse"), ("c2", "endorse"), ("c4", "endorse")),
    }
    got = {a["claim_id"]: a for a in compute_agreement(claims, positions, 3, 0.67, {"c4"})}
    assert got["c1"]["agreement"] == "agreed" and got["c1"]["endorse"] == ["a", "b", "c"]
    assert got["c2"]["agreement"] == "contested"  # 2 of 3 is short of two thirds (0.67)
    assert got["c3"]["agreement"] == "rejected" and got["c3"]["object"] == ["a", "b"]
    assert got["c4"]["agreement"] == "contested"  # everyone endorses, but a challenge on it is open
    assert got["c5"]["agreement"] == "rejected"  # conceded
    assert compute_agreement(claims[:2], positions, 3, 0.6, set())[1]["agreement"] == "agreed"


async def test_contested_claims_are_argued_again_until_the_council_agrees(db, run_row):
    rounds = []

    def script(req):
        if req.metadata.get("phase") != "position":
            return reply("ok")
        first = "Converge, round 1:" in req.messages[-1].content
        rounds.append(first)
        stance = "object" if first and req.metadata["member"] == "Ben" else "endorse"
        return reply(json.dumps({"conclusion": "A leads.", "stances": [{"claim_id": cid, "stance": stance}]}))

    rails = {**QUIET, "debate": {"consensus": 0.9}}
    orch, _, events, _, m = _council(db, run_row, script, rails)
    cid = (await orch.council.make_claim(m["Ada"], "Option A leads."))["claim_id"]
    agreement = await orch.debate.converge()
    assert rounds.count(True) == 3 and rounds.count(False) == 3  # Cleo, Ada and Ben, twice
    assert agreement == [
        {"claim_id": cid, "agreement": "agreed", "endorse": ["m0", "m1", "m2"], "object": []}
    ]
    positions = _of(events, "debate.position")
    assert {(p["member_id"], p["round"]) for p in positions} == {
        (i, r) for i in ("m0", "m1", "m2") for r in (1, 2)
    }
    assert [d["status"] for d in _of(events, "debate.claim_status")] == ["contested", "agreed"]
    (consensus,) = _of(events, "debate.consensus")
    assert consensus["phase"] == "converge" and consensus["round"] == 2
    assert {x["metadata"]["form"] for x in _a2a(events)} >= {"position"}
    # The second round showed each debater the others' first positions, by name.
    ben_round_2 = next(e for e in events if e.type == "debate.position" and e.data["member_id"] == "m2")
    assert ben_round_2


async def test_blocks_and_endorsed_amendments_send_the_text_back(db, run_row):
    def script(req):
        phase, me = req.metadata.get("phase"), req.metadata.get("member")
        task = req.messages[-1].content
        if phase == "cosign" and "## Amendments" in task:
            return reply(json.dumps({"endorse": ["am_1"]}))
        if phase == "cosign" and "v2" not in task:
            if me == "Ada":
                amendment = {"passage": "v1", "replacement": "v2"}
                return reply(json.dumps({"action": "amend", "reason": "Use v2.", "amendment": amendment}))
            return reply(json.dumps({"action": "sign", "reason": "Fine."}))
        if phase == "cosign":
            return reply(json.dumps({"action": "sign", "reason": "Good."}))
        if phase == "compile":
            return reply("# Report v2" if 'Replace "v1" with "v2"' in task else "# Report v1")
        return reply(json.dumps({"conclusion": "ok", "stances": []}))

    orch, _, events, _, _m = _council(db, run_row, script)
    text = await orch.debate.conclude(max_revisions=1)
    assert text.startswith("# Report v2") and "## How the council reached this" in text
    assert "Signed by: Cleo, Ada, Ben" in text
    outcomes = [d["outcome"] for d in _of(events, "debate.consensus") if d["phase"] == "cosign"]
    assert outcomes == ["revise", "signed"]
    first = next(d for d in _of(events, "debate.consensus") if d["phase"] == "cosign")
    assert {s["action"] for s in first["signatures"]} == {"sign", "amend"} and first["quorum_met"]
    assert [d["status"] for d in _of(events, "debate.round") if d["phase"] == "compile"].count(
        "finished"
    ) == 2


# ---------------------------------------------------------------- whole runs, offline
async def test_an_offline_council_debates_and_co_signs_its_conclusion(client, auth, app):
    requests = []

    def responder(req):
        requests.append(req)
        return council_responder(req)

    install(app, "debating", responder)
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "Pick a vector database", "mode": "coding", "provider": "debating", "model": "m"},
    )
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"], timeout=30)
    assert run["status"] == "completed", run
    evs = await events_of(client, auth, run["id"])
    of = lambda t: [e["data"] for e in evs if e["type"] == t]  # noqa: E731

    (assigned,) = of("council.role_assigned")
    assert assigned["secret"] and assigned["stance"] == ADVOCATE
    claims = of("debate.claim")
    assert claims and all(c["evidence"] for c in claims)
    challenges = of("debate.challenge")
    assert any(c["demand"] == "proof" and c["challenger_id"] == assigned["member_id"] for c in challenges)
    responses = of("debate.response")
    assert any(x["stance"] == "defend" and x["evidence"] and not x["unsupported"] for x in responses)
    assert of("debate.resolution")[0]["outcome"] == "accepted"
    assert of("debate.position")
    final = [d for d in of("debate.consensus") if d["phase"] == "cosign"][-1]
    assert final["outcome"] == "signed" and final["quorum_met"]
    assert final["signatures"] and {s["action"] for s in final["signatures"]} == {"sign"}
    assert {c["agreement"] for c in final["claims"]} == {"agreed"}
    phases = {(d["phase"], d["status"]) for d in of("debate.round")}
    assert {("deliberate", "finished"), ("converge", "finished"), ("compile", "finished")} <= phases
    assert ("cosign", "finished") in phases
    done = {d["phase"] for d in of(TASK_EVENT)}
    assert {
        "devils_advocate",
        "deliberate_challenge",
        "deliberate_answer",
        "position",
        "compile",
        "cosign",
    } <= done

    # Every move is an A2A message with a structured data part.
    msgs = [e["data"]["message"] for e in evs if e["type"] == "a2a.message"]
    forms = {x["metadata"]["form"] for x in msgs}
    assert {"claim", "challenge", "response", "resolution", "position", "signature"} <= forms
    assert "stance" not in forms
    for x in msgs:
        if x["metadata"]["form"] in ("claim", "challenge", "response", "resolution", "position", "signature"):
            assert _data_part(x)["type"] == f"argus.{x['metadata']['form']}"

    # The deliverable: the agreed text, how it was reached, and every author's signature.
    result = (await client.get(f"{API}/{run['id']}", headers=auth)).json()["result"]
    assert result.startswith("# Pick a vector database") and "## How the council reached this" in result
    assert "Co-authored by the council" in result and "Signed by:" in result
    # The secret stays with the owner: no other member, overseer or the deliverable ever saw it.
    advocate = assigned["name"]
    for req in requests:
        if req.metadata.get("member") == advocate:
            continue
        for msg in req.messages:
            assert not any(w in msg.content.lower() for w in SECRET_WORDS), (req.metadata, msg.content[:300])
    assert not any(w in result.lower() for w in SECRET_WORDS)
    assert not any(w in json.dumps(msgs).lower() for w in SECRET_WORDS)


async def test_an_offline_research_report_is_co_signed_instead_of_voted(client, auth, app):
    r = await client.post(API, headers=auth, json={"topic": "Solid-state batteries", "mode": "research"})
    run = await wait_status(client, auth, r.json()["id"], timeout=30)
    assert run["status"] == "completed", run
    evs = await events_of(client, auth, run["id"])
    final = [e["data"] for e in evs if e["type"] == "debate.consensus" and e["data"]["phase"] == "cosign"]
    assert final and final[-1]["outcome"] == "signed"
    report = json.loads(
        (
            await client.get(
                f"{API}/{run['id']}/artifacts/content", params={"path": "report.json"}, headers=auth
            )
        ).text
    )
    assert report["author"].startswith("Argus Council: ")
    assert any(s["heading"] == "How the council reached this" for s in report["appendix"])
    stages = {c["stage"] for c in report["quality"]["checkins"]}
    assert "council vote" in stages
    assert "devil" not in run["result"].lower()


async def test_with_debate_off_the_council_synthesises_and_votes_as_before(client, auth, app):
    seen = set()

    def responder(req):
        seen.add(req.metadata.get("phase"))
        seen.update(t.name for t in req.tools)
        return council_responder(req)

    install(app, "classic", responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Pick a vector database",
            "mode": "coding",
            "provider": "classic",
            "model": "m",
            "guardrails": {"debate": {"enabled": False}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"], timeout=30)
    assert run["status"] == "completed", run
    evs = await events_of(client, auth, run["id"])
    types = {e["type"] for e in evs}
    assert {"vote.opened", "vote.cast", "vote.closed"} <= types
    assert not {t for t in types if t.startswith("debate.") or t == "council.role_assigned"}
    assert "synthesize" in seen and not seen & {"position", "cosign", "compile", "make_claim"}
    vote = next(e["data"] for e in evs if e["type"] == "vote.opened")
    assert [o["id"] for o in vote["options"]] == ["approve", "revise"]


def test_the_debate_module_is_wired_into_the_orchestrator():
    assert Debate.conclude and CouncilOrchestrator.run_report


# ---------------------------------------------------------------- cost controls
def _pool_of(*models):
    from argus.services.autocouncil import Candidate, model_tier

    async def pool(ctx, providers):
        return [
            Candidate("anthropic", m, model_tier(m), "api_key") for m in models if "anthropic" in providers
        ]

    return pool


async def test_light_moves_use_the_newest_cheap_strong_model_of_the_seats_provider(db, run_row, monkeypatch):
    import dataclasses

    import argus.tools.spawn as spawn
    from argus.services.llm import Route

    monkeypatch.setattr(
        spawn,
        "_pool",
        _pool_of("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5"),
    )
    orch, ctx, *_ = _council(db, run_row, lambda req: reply("ok"))
    opus = dataclasses.replace(_member("m9", "Ida", "analyst"), provider="anthropic", model="claude-opus-5-5")
    light = await orch.debate.light_seat(opus)
    # Haiku 5.5 is the cheapest above the reasoning floor (Haiku 4.5 is cheaper per call only in
    # name: it is below the floor); the seat's own model stays the fallback; nothing else changes.
    assert light.model == "claude-haiku-5-5" and light.fallbacks[0] == Route("anthropic", "claude-opus-5-5")
    assert (light.id, light.name, light.system_prompt, light.stance) == (
        opus.id,
        opus.name,
        opus.system_prompt,
        None,
    )
    # A floor no cheaper model meets, "seat" mode and CLI seats keep the seat's own model.
    orch.debate._light.clear()
    ctx.engine.rails.debate.light_min_reasoning = 96
    assert (await orch.debate.light_seat(opus)).model == "claude-opus-5-5"
    orch.debate._light.clear()
    ctx.engine.rails.debate.light_min_reasoning = 70
    ctx.engine.rails.debate.light_model = "seat"
    assert (await orch.debate.light_seat(opus)) is opus
    orch.debate._light.clear()
    ctx.engine.rails.debate.light_model = "latest_cheapest"
    cli = dataclasses.replace(opus, provider="claude-cli")
    assert (await orch.debate.light_seat(cli)) is cli


async def test_light_moves_are_one_call_without_tools_and_answers_keep_read_tools(db, run_row):
    asked = []

    def script(req):
        phase = req.metadata.get("phase")
        asked.append((phase, req.metadata.get("member"), sorted(t.name for t in req.tools)))
        if phase == "deliberate_challenge" and req.metadata["member"] == "Ada":
            return reply(
                json.dumps({"challenges": [{"target": "Ben", "objection": "Source?", "demand": "proof"}]})
            )
        if phase == "deliberate_answer":
            return reply(json.dumps({"responses": []}))
        return reply("{}")

    rails = {**QUIET, "debate": {"rounds": 1}}
    orch, _, events, _, m = _council(db, run_row, script, rails)
    await orch.council.make_claim(m["Ben"], "Costs fall 20% a year.")
    await orch.debate.deliberate()
    challenges = [a for a in asked if a[0] == "deliberate_challenge"]
    answers = [a for a in asked if a[0] == "deliberate_answer"]
    assert challenges and all(tools == [] for _, _, tools in challenges)  # one call, no tools
    assert answers and all("respond_to_challenge" not in tools for _, _, tools in answers)
    assert all(
        t
        in {
            "notes_read",
            "web_fetch",
            "web_search",
            "knowledge_search",
            "knowledge_read",
            "memory_search",
            "artifact_read",
            "market_data",
            "news_search",
            "graph_query",
            "graph_read",
        }
        for _, _, tools in answers
        for t in tools
    )
    (cost,) = _of(events, "debate.cost")
    assert cost["phase"] == "deliberate" and cost["tasks"] == len(challenges) + len(answers)


async def test_a_debater_shown_nothing_new_is_not_asked_again(db, run_row):
    asked = []

    def script(req):
        phase = req.metadata.get("phase")
        if phase == "deliberate_challenge" and req.messages[-1].content.startswith("Your task"):
            asked.append(req.metadata["member"])  # each task's first call (not oversight retries)
            if "round 1" in req.messages[-1].content and req.metadata["member"] == "Ada":
                return reply(
                    json.dumps({"challenges": [{"target": "Ben", "objection": "Source?", "demand": "proof"}]})
                )
        return reply("{}")  # the answer says nothing: the challenge stays open, nothing changes

    rails = {**QUIET, "debate": {"rounds": 3}}
    orch, *_, m = _council(db, run_row, script, rails)
    await orch.council.make_claim(m["Ben"], "Costs fall 20% a year.")
    await orch.debate.deliberate()
    # Round 1: all three debaters. Round 2: the new challenge changed everyone's view. Round 3:
    # nobody was shown anything new, so nobody is asked again.
    assert sorted(asked) == sorted(["Cleo", "Ada", "Ben"] * 2)
