import pytest

from argus.core.config import Settings
from argus.providers.mock import MockProvider, council_responder
from tests.test_runs import wait_status


async def test_metrics_exposed_in_dev(client, auth):
    await client.post("/api/v1/runs", headers=auth, json={"topic": "m", "mode": "chat", "size": 2})
    text = (await client.get("/metrics")).text
    assert "argus_http_requests_total" in text and 'method="POST"' in text
    assert "argus_runs_started_total" in text


async def test_metrics_token_and_production(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from argus.main import create_app

    base = dict(database_url=f"sqlite+aiosqlite:///{tmp_path}/m.db", secret_key="s" * 40)
    for settings, headers, expected in [
        (Settings(env="test", metrics_token="tok-123", **base), {}, 401),
        (Settings(env="test", metrics_token="tok-123", **base), {"Authorization": "Bearer tok-123"}, 200),
        (Settings(env="production", **base), {}, 404),
    ]:
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
        ):
            assert (await c.get("/metrics", headers=headers)).status_code == expected


async def test_audit_log_records_actions_and_failures(client, auth):
    await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong-password"})
    await client.post("/api/v1/councils", headers=auth, json={"name": "audited"})
    await client.post(
        "/api/v1/providers", headers=auth, json={"name": "p", "kind": "openai", "api_key": "sk-hide-me-123"}
    )
    import asyncio

    for _ in range(100):  # audit rows are written asynchronously
        events = (await client.get("/api/v1/audit", headers=auth)).json()["events"]
        if len(events) >= 2:
            break
        await asyncio.sleep(0.05)
    actions = [e["action"] for e in events]
    assert "POST /councils" in actions and "POST /providers" in actions
    assert "sk-hide-me" not in str(events)
    # failed logins are recorded even without an actor (visible to admins)
    every = (
        await client.get("/api/v1/audit", headers=auth, params={"all": "true", "success": "false"})
    ).json()["events"]
    login = next(e for e in every if e["action"] == "POST /auth/login")
    assert (
        login["detail"]["email"] == "admin@example.com"
        and login["detail"]["status"] == 401
        and login["actor_id"] is None
    )


async def test_audit_all_requires_admin(client, auth):
    from tests.conftest import register_and_login

    other = await register_and_login(client, "nonadmin@example.com")
    assert (await client.get("/api/v1/audit", headers=other, params={"all": "true"})).status_code == 403
    assert (await client.get("/api/v1/audit", headers=other)).status_code == 200


async def test_insights_aggregate_runs(client, auth, app):
    from argus.providers.types import CompletionResponse, Usage

    def responder(req):
        out = council_responder(req)
        if req.metadata.get("phase") == "execute":
            return (
                {"content": "", "tool_calls": [{"name": "nope_tool", "arguments": {}}]}
                if not any(m.role == "tool" for m in req.messages)
                else {"content": "done"}
            )
        return CompletionResponse(
            provider="ins",
            model="m1",
            content=out["content"],
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01),
        )

    app.state.llm.system["ins"] = MockProvider("ins", responder=responder)
    for _ in range(2):
        r = await client.post(
            "/api/v1/runs",
            headers=auth,
            json={"topic": "t", "mode": "chat", "size": 2, "provider": "ins", "model": "m1"},
        )
        await wait_status(client, auth, r.json()["id"])
    data = (await client.get("/api/v1/insights", headers=auth, params={"days": 7})).json()
    assert data["totals"]["runs"] == 2 and data["totals"]["success_rate"] == 100.0
    assert data["runs_by_mode"] == {"chat": 2}
    model = next(m for m in data["models"] if m["model"] == "ins/m1")
    assert model["calls"] > 0 and model["cost_usd"] > 0
    tool = next(t for t in data["tools"] if t["tool"] == "nope_tool")
    assert tool["calls"] == 2 and tool["error_rate"] == 100.0


@pytest.mark.parametrize("days", [0, 400])
async def test_insights_validates_window(client, auth, days):
    assert (await client.get("/api/v1/insights", headers=auth, params={"days": days})).status_code == 422
