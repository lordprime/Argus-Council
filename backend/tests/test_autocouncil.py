"""Task-based council composition and per-seat model choice."""

import json
from collections import Counter
from types import SimpleNamespace

from argus.providers.base import ProviderError
from argus.providers.mock import MockProvider, council_responder
from argus.services.autocouncil import (
    BUDGET_SHARE,
    ROLE_NEEDS,
    Candidate,
    PlannedSeat,
    assign_models,
    choose_assignee,
    estimated_spend,
    fit,
    heuristic_plan,
    model_tier,
    parse_planner_reply,
    pick_model,
    reassign_roster,
    shed_for_budget,
)
from tests.test_runs import wait_status

PLAN = "/api/v1/runs/plan"


def test_model_tiers_by_price_then_name():
    assert model_tier("claude-opus-5-5") == "strong"
    assert model_tier("claude-sonnet-5-5") == "balanced"
    assert model_tier("claude-haiku-5-5") == "fast"
    assert model_tier("gpt-5") == "strong" and model_tier("gpt-5-mini") == "fast"
    assert model_tier("gemini-2.5-pro") == "strong" and model_tier("gemini-2.5-flash") == "fast"
    assert model_tier("llama-3.3-70b-versatile") == "balanced"
    assert model_tier("opus") == "strong" and model_tier("haiku") == "fast"  # Claude Code CLI aliases


def test_pick_model_weighs_competence_against_cost():
    pool = [
        Candidate("a", "claude-opus-5-5", "strong", "api_key"),
        Candidate("a", "claude-haiku-5-5", "fast", "api_key"),
        Candidate("b", "gpt-5", "strong", "api_key", preferred=True),
        Candidate("c", "sonnet", "balanced", "account"),
    ]
    # The chair needs strong reasoning; gpt-5 is competent, cheaper per token, and the user's pick.
    assert pick_model("chair", pool).model == "gpt-5"
    # Monitors read every thought: the fast, cheap model wins.
    assert pick_model("monitor", pool).model == "claude-haiku-5-5"
    # Strategy moves the writer along the competence/cost curve.
    quality = fit("writer", pick_model("writer", pool, strategy="quality"))
    economy = fit("writer", pick_model("writer", pool, strategy="economy"))
    assert quality.competence >= economy.competence and quality.seat_usd >= economy.seat_usd
    # A cheap model below the role's floor never beats a competent one, preferred or not.
    weak = [Candidate("x", "mock-1", "balanced", "api_key", preferred=True), pool[0]]
    assert pick_model("chair", weak).model == "claude-opus-5-5"
    assert pick_model("chair", []) is None


def test_fit_accounts_for_subscriptions_and_self_hosting():
    hosted = fit("writer", Candidate("ollama", "llama3:70b", "balanced", "api_key", self_hosted=True))
    assert hosted.seat_usd == 0 and hosted.candidate.price.basis == "self_hosted"
    cli = fit("writer", Candidate("claude-cli", "sonnet", "balanced", "account"))
    assert cli.seat_usd == 0 and cli.candidate.price.basis == "subscription"
    unknown = Candidate("x", "acme-large", "strong", "api_key")
    assert unknown.price.basis == "estimated" and unknown.profile.source == "heuristic"


def test_assign_models_fits_the_council_to_the_budget():
    pool = [
        Candidate("a", "claude-opus-5-5", "strong", "api_key"),
        Candidate("a", "claude-sonnet-5-5", "balanced", "api_key"),
        Candidate("a", "claude-haiku-5-5", "fast", "api_key"),
    ]

    def seats():
        return [PlannedSeat(role=r, name=r.title()) for r in ("chair", "writer", "analyst", "monitor")]

    free = seats()
    assign_models(free, pool, None, strategy="quality")
    assert all(s.model == "claude-opus-5-5" for s in free[:3]) and free[0].competence >= 90
    tight = seats()
    notes = assign_models(tight, pool, None, strategy="quality", budget_usd=1.2)
    assert estimated_spend(tight) <= 1.2 * BUDGET_SHARE < estimated_spend(free)
    assert any("to fit the $1.2 budget" in n for n in notes)
    # Downgrades never drop a seat below its role's floor while a competent option exists.
    assert all(s.competence >= ROLE_NEEDS[s.role].floor for s in tight)
    impossible = seats()
    notes = assign_models(impossible, pool, None, budget_usd=0.01)
    assert any("raise the budget" in n for n in notes)


def test_shed_for_budget_drops_repeated_roles_first_and_keeps_the_chair_and_an_overseer():
    seats = [
        PlannedSeat(role="chair", name="Chair", est_usd=0.5),
        PlannedSeat(role="researcher", name="Researcher", est_usd=0.4),
        PlannedSeat(role="researcher", name="Researcher 2", est_usd=0.4),
        PlannedSeat(role="writer", name="Writer", est_usd=0.6),
        PlannedSeat(role="monitor", name="Monitor", est_usd=0.1, overseer=True),
    ]
    notes = shed_for_budget(seats, 1.6)
    assert [s.name for s in seats] == ["Chair", "Researcher", "Monitor"] or "Researcher 2" not in [
        s.name for s in seats
    ]
    assert seats[0].role == "chair" and any(s.overseer for s in seats) and notes


def test_choose_assignee_matches_the_work_to_the_member():
    members = [
        SimpleNamespace(id="1", role="analyst", provider="a", model="claude-opus-5-5"),
        SimpleNamespace(id="2", role="writer", provider="a", model="claude-sonnet-5-5"),
        SimpleNamespace(id="3", role="coder", provider="a", model="claude-sonnet-5-5"),
    ]
    load = Counter()
    assert choose_assignee("Implement the CSV parser and its tests", members, load).role == "coder"
    assert choose_assignee("Weigh the trade-offs between the options", members, load).role == "analyst"
    assert choose_assignee("Draft the final report", members, load).role == "writer"
    load["2"] = 5  # a busy member yields the next step
    assert choose_assignee("Draft the final report", members, load).role != "writer"


def test_reassign_roster_gives_child_seats_the_parent_models_that_fit():
    roster = [
        {"name": "Chair", "role": "chair", "provider": "a", "model": "claude-haiku-5-5"},
        {"name": "Monitor", "role": "monitor", "provider": "a", "model": "claude-haiku-5-5"},
    ]
    routes = [("a", "claude-haiku-5-5"), ("a", "claude-opus-5-5")]
    reassign_roster(roster, routes, budget_usd=5)
    assert roster[0]["model"] == "claude-opus-5-5" and roster[1]["model"] == "claude-haiku-5-5"


def test_heuristic_plan_follows_the_task():
    seats = heuristic_plan("Implement a CLI that converts CSV to JSON, with tests", "", "coding", 12)
    roles = [s.role for s in seats]
    assert roles[0] == "chair" and {"architect", "coder", "tester", "critic", "monitor"} <= set(roles)
    research = [
        s.role for s in heuristic_plan("Survey recent papers on solid-state batteries", "", "research", 12)
    ]
    assert research.count("researcher") == 2 and "analyst" in research
    small = heuristic_plan(
        "Research and write a security report with metrics and a slide deck", "", "general", 4
    )
    assert len(small) == 4 and small[0].role == "chair" and small[-1].role == "monitor"
    names = [s.name for s in heuristic_plan("Survey papers", "", "research", 12)]
    assert "Researcher" in names and "Researcher 2" in names


def test_parse_planner_reply_validates_and_completes_the_council():
    reply = "Here you go:\n" + json.dumps(
        {
            "rationale": "Two angles of research",
            "seats": [
                {"role": "researcher", "focus": "chemistry", "reason": "r"},
                {"role": "researcher", "focus": "markets"},
                {"role": "wizard", "focus": "not a role"},
                {"role": "chair"},
                {"role": "chair"},
            ],
        }
    )
    seats, rationale = parse_planner_reply(reply, "research", 12)
    assert [s.role for s in seats] == ["researcher", "researcher", "chair", "monitor"]
    assert (
        seats[1].name == "Researcher 2"
        and seats[0].focus == "chemistry"
        and rationale == "Two angles of research"
    )
    assert parse_planner_reply("no json", "research", 12) is None
    assert parse_planner_reply('{"seats": [{"role": "wizard"}]}', "research", 12) is None


def _planner(app, seats):
    """A system provider whose planning call returns ``seats`` and which then runs the council."""

    def responder(req):
        if req.metadata.get("purpose") == "plan_council":
            return {"content": json.dumps({"rationale": "fits the task", "seats": seats})}
        return council_responder(req)

    app.state.llm.system["planner"] = MockProvider("planner", responder=responder)


async def test_plan_endpoint_composes_with_the_llm_and_tiers_models(client, auth, app):
    _planner(app, [{"role": "researcher", "focus": "papers"}, {"role": "analyst"}, {"role": "monitor"}])
    r = await client.post(
        "/api/v1/providers",
        headers=auth,
        json={"name": "anthropic", "kind": "anthropic", "api_key": "sk-ant-test-key-123456"},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        PLAN,
        headers=auth,
        json={"topic": "Battery research", "mode": "research", "provider": "planner", "model": "m"},
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["planner"] == "llm" and plan["planner_route"] == {"provider": "planner", "model": "m"}
    by_role = {s["role"]: s for s in plan["seats"]}
    assert list(by_role) == ["chair", "researcher", "analyst", "monitor"]  # the chair is always added
    assert by_role["chair"]["model"] == "claude-opus-5-5" and by_role["chair"]["tier"] == "strong"
    assert by_role["monitor"]["model"] == "claude-haiku-5-5"
    assert by_role["researcher"]["focus"] == "papers"
    # Every seat says why: competence for its role and expected spend, and the plan totals it.
    assert by_role["chair"]["competence"] >= 80 and by_role["chair"]["cost_basis"] == "listed"
    assert plan["strategy"] == "balanced" and plan["budget_usd"] == 5.0
    assert plan["estimated_usd"] == round(sum(s["est_usd"] for s in plan["seats"]), 4) > 0
    # The economy strategy never plans a costlier council than quality.
    costs = {}
    for strategy in ("quality", "economy"):
        r = await client.post(
            PLAN,
            headers=auth,
            json={
                "topic": "Battery research",
                "mode": "research",
                "roles": ["chair", "writer", "analyst"],
                "model_strategy": strategy,
            },
        )
        costs[strategy] = r.json()["estimated_usd"]
    assert costs["economy"] <= costs["quality"]

    # Fixed roles, models only.
    r = await client.post(
        PLAN, headers=auth, json={"topic": "t", "roles": ["critic", "writer"], "auto_council": True}
    )
    assert [s["role"] for s in r.json()["seats"]] == ["critic", "writer"] and r.json()["planner"] == "roster"


async def test_planner_failure_falls_back_to_keywords(client, auth, app):
    class Down(MockProvider):
        async def complete(self, request):
            raise ProviderError("down", provider="down")

    app.state.llm.system["down"] = Down("down")
    r = await client.post(
        PLAN,
        headers=auth,
        json={"topic": "Fix the login bug and add tests", "mode": "coding", "provider": "down", "model": "x"},
    )
    plan = r.json()
    assert plan["planner"] == "heuristic" and "coder" in [s["role"] for s in plan["seats"]]
    assert any("could not be reached" in n for n in plan["notes"])


async def test_auto_council_run_and_planned_seats(client, auth, app):
    _planner(
        app, [{"role": "analyst", "focus": "weigh the options"}, {"role": "writer"}, {"role": "monitor"}]
    )
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={
            "topic": "Pick a database",
            "mode": "general",
            "provider": "planner",
            "model": "m",
            "auto_council": True,
            "auto_models": True,
        },
    )
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    snap = run["council_snapshot"]
    assert snap["name"] == "Auto council" and snap["plan"]["planner"] == "llm"
    assert [m["role"] for m in snap["members"]] == ["chair", "analyst", "writer", "monitor"]
    analyst = snap["members"][1]
    assert (
        analyst["config"]["focus"] == "weigh the options" and "weigh the options" in analyst["system_prompt"]
    )

    # Explicit seats (what the composer sends after a preview) are used as given.
    seats = [
        {"role": "chair", "provider": "mock", "model": "mock-1"},
        {"role": "researcher", "focus": "prior art", "provider": "mock", "model": "mock-1"},
        {"role": "researcher", "provider": "mock", "model": "mock-1"},
    ]
    r = await client.post(
        "/api/v1/runs", headers=auth, json={"topic": "t", "mode": "research", "seats": seats}
    )
    assert r.status_code == 201, r.text
    names = (
        [m["name"] for m in r.json()["council_snapshot"]["members"]]
        if "council_snapshot" in r.json()
        else None
    )
    detail = (await client.get(f"/api/v1/runs/{r.json()['id']}", headers=auth)).json()
    assert [m["name"] for m in detail["council_snapshot"]["members"]] == [
        "Chair",
        "Researcher",
        "Researcher 2",
    ]
    assert names is None or names == ["Chair", "Researcher", "Researcher 2"]
    bad = {"topic": "t", "seats": [{"role": "wizard", "provider": "mock", "model": "mock-1"}]}
    assert (await client.post("/api/v1/runs", headers=auth, json=bad)).status_code == 422


async def test_auto_council_with_only_the_mock_provider_completes(client, auth):
    """The composer's defaults (Auto council, Auto models) with no provider keys, as in CI's UI end-to-end run."""
    import time

    body = {
        "topic": "Solid-state batteries in 2026",
        "description": "Focus on energy density, cost and timelines.",
        "mode": "research",
        "size": 8,
        "auto_council": True,
        "auto_models": True,
    }
    start = time.monotonic()
    r = await client.post("/api/v1/runs", headers=auth, json=body)
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    assert time.monotonic() - start < 15
    snap = run["council_snapshot"]
    assert snap["plan"]["planner"] == "heuristic"  # the mock's reply is not a plan
    assert {m["provider"] for m in snap["members"]} == {"mock"} and len(snap["members"]) <= 8
