"""Task-based council composition and per-seat model choice."""

import json

from argus.providers.base import ProviderError
from argus.providers.mock import MockProvider, council_responder
from argus.services.autocouncil import (
    Candidate,
    heuristic_plan,
    model_tier,
    parse_planner_reply,
    pick_model,
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


def test_pick_model_matches_role_tier_and_prefers_the_users_provider():
    pool = [
        Candidate("a", "claude-opus-5-5", "strong", "api_key"),
        Candidate("a", "claude-haiku-5-5", "fast", "api_key"),
        Candidate("b", "gpt-5", "strong", "api_key", preferred=True),
        Candidate("c", "sonnet", "balanced", "account"),
    ]
    assert pick_model("chair", pool).model == "gpt-5"  # strong, and the user's provider
    assert pick_model("monitor", pool).model == "claude-haiku-5-5"  # high-volume overseer: fast
    assert pick_model("writer", pool).model == "sonnet"  # balanced
    # Missing tier: the nearest one, stronger first.
    assert pick_model("writer", [c for c in pool if c.tier != "balanced"]).tier == "strong"
    assert pick_model("chair", []) is None


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
    assert by_role["researcher"]["provider"] == "planner" and by_role["researcher"]["focus"] == "papers"

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
