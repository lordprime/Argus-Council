"""Feature switches: installed on the server, turned on and off by an admin in the app."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from argus.core.config import Settings
from argus.db.models import AuditEvent, FeatureSwitch
from argus.main import create_app
from argus.providers.cli.errors import CLIError
from argus.providers.types import CompletionRequest, Message
from argus.services.cli_agents import CLIAgents, CLIAgentsDisabled
from argus.services.features import BROWSER_OFF_MESSAGE, CLI_OFF_MESSAGE, FeatureSwitches
from argus.tools import browser as browser_mod
from argus.tools.browser import Browse, browser_tools
from tests.conftest import _reset_state, register_and_login
from tests.test_browse_tool import Ctx, FakeBrowser
from tests.test_cli_agents_api import FakeRunner

API = "/api/v1"
FEATURES = f"{API}/admin/features"
ADMIN_OFF = "CLI agents are turned off by an admin (Settings → Features)"


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/argus.db",
        secret_key="test-secret-key-that-is-long-enough-1234567890",
        rate_limit_per_minute=10_000,
        data_dir=str(tmp_path / "data"),
        **kw,
    )


def _healthy(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/health"
    return httpx.Response(200, json={"status": "ok"})


INSTALLED = {
    "browser_url": "unix:/nonexistent/browser.sock",
    "browser_token": "browser-token",
    "cli_agents_enabled": True,
    "runner_url": "unix:/nonexistent/runner.sock",
    "runner_token": "runner-token",
}


@pytest.fixture
async def feat_app(tmp_path):
    app = create_app(_settings(tmp_path, **INSTALLED))
    async with app.router.lifespan_context(app):
        app.state.cli_agents.transport = FakeRunner()
        app.state.features.http_transport = httpx.MockTransport(_healthy)
        yield app
        await _reset_state(app)


@pytest.fixture
async def feat_client(feat_app):
    async with AsyncClient(transport=ASGITransport(app=feat_app), base_url="http://test") as c:
        yield c


async def _drain(app) -> None:
    while pending := [t for t in app.state.background if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------- the service
def test_defaults_no_row_is_on_and_not_installed_is_off():
    features = FeatureSwitches(Settings(), None)
    assert features.switch("browser") and features.switch("cli_agents")
    assert not features.installed("browser") and not features.installed("cli_agents")
    assert not features.is_on("browser") and not features.is_on("cli_agents")

    installed = FeatureSwitches(Settings(**INSTALLED), None)
    assert installed.is_on("browser") and installed.is_on("cli_agents")
    # CLI agents need both the setting and a runner; the dev runner counts.
    assert not FeatureSwitches(Settings(cli_agents_enabled=True), None).installed("cli_agents")
    assert not FeatureSwitches(Settings(runner_url="http://runner"), None).installed("cli_agents")
    assert FeatureSwitches(Settings(cli_agents_enabled=True, runner_local=True), None).installed("cli_agents")


async def test_set_and_refresh(app):
    features: FeatureSwitches = app.state.features
    admin = uuid.uuid4()
    assert await features.set("browser", False, None) is True  # previous: on (no row)
    assert features.switch("browser") is False
    assert await features.set("browser", False, None) is False

    # Another process changed it: refresh picks it up.
    async with app.state.db.sessionmaker() as s:
        (await s.get(FeatureSwitch, "browser")).enabled = True
        s.add(FeatureSwitch(key="cli_agents", enabled=False, updated_by_id=None))
        await s.commit()
    assert features.switch("browser") is False
    await features.refresh()
    assert features.switch("browser") is True and features.switch("cli_agents") is False

    with pytest.raises(ValueError):
        await features.set("nope", True, admin)


async def test_a_refresh_that_read_before_a_change_does_not_undo_it(app):
    features: FeatureSwitches = app.state.features
    real = features.sessionmaker

    class Racing:
        """A session whose read lands just before an admin's change is committed."""

        async def __aenter__(self):
            self.inner = real()
            self.s = await self.inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self.inner.__aexit__(*exc)

        async def execute(self, stmt):
            result = await self.s.execute(stmt)
            async with real() as other:
                await features.set("browser", False, None, db=other)
            return result

    features.sessionmaker = lambda: Racing()  # type: ignore[assignment]
    try:
        await features.refresh()
    finally:
        features.sessionmaker = real
    assert features.switch("browser") is False
    await features.refresh()
    assert features.switch("browser") is False


async def test_refresh_failure_keeps_the_last_state():
    class Broken:
        def __call__(self):
            raise RuntimeError("database down")

    features = FeatureSwitches(Settings(), Broken())  # type: ignore[arg-type]
    features._switches = {"browser": False}
    await features.refresh()
    assert features.switch("browser") is False


async def test_probe_reachable_unreachable_not_installed():
    seen: list[str] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    http = Settings(browser_url="http://browser:8000/", cli_agents_enabled=True, runner_url="unix:/x.sock")
    features = FeatureSwitches(http, None, http_transport=httpx.MockTransport(ok))
    assert await features.probe("browser") == "reachable"
    assert await features.probe("cli_agents") == "reachable"
    assert seen == ["http://browser:8000/health", "http://localhost/health"]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    refused = FeatureSwitches(http, None, http_transport=httpx.MockTransport(down))
    assert await refused.probe("browser") == "unreachable"
    failing = httpx.MockTransport(lambda r: httpx.Response(503))
    assert await FeatureSwitches(http, None, http_transport=failing).probe("cli_agents") == "unreachable"

    async def hung(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    slow = Settings(browser_url="http://browser:8000", health_check_timeout_s=0.05)
    assert await FeatureSwitches(slow, None, http_transport=httpx.MockTransport(hung)).probe("browser") == (
        "unreachable"
    )

    # A real Unix socket that does not exist: unreachable, not an exception.
    no_socket = FeatureSwitches(Settings(browser_url="unix:/nonexistent/b.sock"), None)
    assert await no_socket.probe("browser") == "unreachable"
    assert await FeatureSwitches(Settings(), None).probe("browser") == "not_installed"
    assert await FeatureSwitches(Settings(), None).probe("cli_agents") == "not_installed"
    # The development runner has no URL: it runs in-process.
    local = Settings(cli_agents_enabled=True, runner_local=True)
    assert await FeatureSwitches(local, None).probe("cli_agents") == "reachable"


# ---------------------------------------------------------------- the API
async def test_get_features_defaults(feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    r = await feat_client.get(FEATURES, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["key"] for f in body] == ["browser", "cli_agents"]
    browser, cli = body
    assert browser == {
        "key": "browser",
        "label": "Live browser",
        "installed": True,
        "status": "reachable",
        "enabled": True,
        "active": True,
        "install_command": "./scripts/deploy.sh --with browser",
        "updated_at": None,
        "updated_by": None,
    }
    assert cli["label"] == "CLI agents" and cli["install_command"] == "./scripts/deploy.sh --with cli-agents"
    assert cli["active"] is True


async def test_not_installed_features(client, auth):
    body = (await client.get(FEATURES, headers=auth)).json()
    assert [(f["key"], f["installed"], f["status"], f["enabled"], f["active"]) for f in body] == [
        ("browser", False, "not_installed", True, False),
        ("cli_agents", False, "not_installed", True, False),
    ]
    r = await client.patch(f"{FEATURES}/browser", json={"enabled": True}, headers=auth)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["message"] == (
        "Live browser is not installed on this server: run ./scripts/deploy.sh --with browser"
    )
    r = await client.patch(f"{FEATURES}/cli_agents", json={"enabled": True}, headers=auth)
    assert r.status_code == 409
    assert r.json()["error"]["message"].endswith("run ./scripts/deploy.sh --with cli-agents")
    # Turning off a feature that is not installed is allowed (it stays off once installed).
    r = await client.patch(f"{FEATURES}/browser", json={"enabled": False}, headers=auth)
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["active"] is False


async def test_patch_feature_records_who_and_audits(feat_app, feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    r = await feat_client.patch(f"{FEATURES}/browser", json={"enabled": False}, headers=admin)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["key"] == "browser" and out["enabled"] is False and out["active"] is False
    assert out["installed"] is True and out["updated_by"] == "admin@example.com"
    assert out["updated_at"] is not None
    assert feat_app.state.features.is_on("browser") is False
    listed = {f["key"]: f for f in (await feat_client.get(FEATURES, headers=admin)).json()}
    assert listed["browser"]["enabled"] is False and listed["browser"]["updated_by"] == "admin@example.com"
    assert listed["cli_agents"]["enabled"] is True

    r = await feat_client.patch(f"{FEATURES}/browser", json={"enabled": True}, headers=admin)
    assert r.status_code == 200 and r.json()["active"] is True

    await _drain(feat_app)
    async with feat_app.state.db.sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action == "PATCH /admin/features/{key}")
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )
    assert [(e.target_id, e.success, e.detail["feature"], e.detail["enabled"]) for e in rows] == [
        ("browser", True, "browser", {"from": True, "to": False}),
        ("browser", True, "browser", {"from": False, "to": True}),
    ]


async def test_feature_api_errors(feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    member = await register_and_login(feat_client, "member@example.com")
    assert (await feat_client.get(FEATURES, headers=member)).status_code == 403
    r = await feat_client.patch(f"{FEATURES}/browser", json={"enabled": False}, headers=member)
    assert r.status_code == 403
    assert (await feat_client.get(FEATURES)).status_code == 401

    r = await feat_client.patch(f"{FEATURES}/sandbox", json={"enabled": False}, headers=admin)
    assert r.status_code == 404
    for body in ({"enabled": False, "installed": True}, {"enabled": "no"}, {}):
        r = await feat_client.patch(f"{FEATURES}/browser", json=body, headers=admin)
        assert r.status_code == 422, body


# ---------------------------------------------------------------- the live browser
async def test_browse_is_left_out_of_runs_while_switched_off(feat_app, feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    runs = feat_app.state.runs
    assert runs.tool_factory(None).get("browse") is not None
    await feat_client.patch(f"{FEATURES}/browser", json={"enabled": False}, headers=admin)
    assert runs.tool_factory(None).get("browse") is None
    await feat_client.patch(f"{FEATURES}/browser", json={"enabled": True}, headers=admin)
    assert runs.tool_factory(None).get("browse") is not None


async def test_browse_is_refused_mid_run_once_switched_off(feat_app, feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    tool = feat_app.state.runs.tool_factory(None).get("browse")
    fake = FakeBrowser()
    tool.browser = fake
    await feat_client.patch(f"{FEATURES}/browser", json={"enabled": False}, headers=admin)
    result = await tool.run(tool.Args(action="snapshot"), Ctx())
    assert result.is_error and result.content == BROWSER_OFF_MESSAGE
    assert fake.calls == []  # the service was never asked


async def test_browse_checks_its_switch_on_every_call(monkeypatch):
    async def record_call(ctx, **entry):
        ctx.ledger.append(entry)

    monkeypatch.setattr(browser_mod, "record_call", record_call)
    on = {"v": True}

    async def public(host):
        return ["93.184.216.34"]

    fake = FakeBrowser()
    tool = Browse(fake, resolver=public, enabled=lambda: on["v"])
    assert not (await tool.run(tool.Args(action="open", url="https://example.com/"), Ctx())).is_error
    on["v"] = False
    result = await tool.run(tool.Args(action="open", url="https://example.com/"), Ctx())
    assert result.is_error and result.content == "The live browser was turned off by an admin"
    # Backward compatible: no callable means always on.
    assert browser_tools(Settings(browser_url="http://b", browser_token="t"))[0].enabled is None


async def test_run_start_refreshes_the_switches(feat_app):
    features: FeatureSwitches = feat_app.state.features
    assert features.is_on("browser")
    async with feat_app.state.db.sessionmaker() as s:
        s.add(FeatureSwitch(key="browser", enabled=False))
        await s.commit()
    assert features.is_on("browser")  # cached until the next run starts

    seen: list[bool] = []

    def factory(run):
        seen.append(features.is_on("browser"))
        raise RuntimeError("stop here")

    feat_app.state.runs.tool_factory = factory
    run = SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        topic="t",
        description="",
        mode="chat",
        outputs=[],
        guardrails={},
        resumed_from_id=None,
        knowledge_base_ids=[],
        council_snapshot={},
        council_id=None,
    )
    feat_app.state.runs._shared_knowledge = _no_shared  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="stop here"):
        await feat_app.state.runs._prepare(run, SimpleNamespace(emit=None), 0, None, [])
    assert seen == [False]


async def _no_shared(run):
    return []


# ---------------------------------------------------------------- CLI agents
def _cli(features: FeatureSwitches | None) -> CLIAgents:
    settings = Settings(cli_agents_enabled=True)
    return CLIAgents(settings, None, transport=FakeRunner(), features=features)  # type: ignore[arg-type]


async def test_cli_seat_is_refused_per_turn_once_switched_off():
    features = FeatureSwitches(Settings(**INSTALLED), None)
    cli = _cli(features)
    assert cli.enabled and _cli(None).enabled
    owner = uuid.uuid4()
    provider = cli.provider_for(owner, "claude-cli")  # built (and cached) while on
    await features.set("cli_agents", False, None)
    assert not cli.enabled
    request = CompletionRequest(
        model="sonnet",
        messages=[Message(role="user", content="hi")],
        metadata={"run_id": "r1", "member_id": "m1"},
    )
    with pytest.raises(CLIError) as err:
        await provider.complete(request)
    assert ADMIN_OFF in str(err.value)
    assert err.value.category == "unavailable" and err.value.fallback
    assert cli.transport.calls == []  # the runner was never asked

    with pytest.raises(CLIError) as err:
        cli.provider_for(owner, "claude-cli")
    assert ADMIN_OFF in str(err.value) and err.value.fallback
    with pytest.raises(CLIAgentsDisabled) as disabled:
        cli.require_enabled()
    assert disabled.value.code == "cli_agents_disabled" and ADMIN_OFF in str(disabled.value)
    assert CLI_OFF_MESSAGE == ADMIN_OFF

    # The env setting still wins over the switch: off there gives the env message.
    off_env = CLIAgents(Settings(cli_agents_enabled=False), None, features=features)
    with pytest.raises(CLIAgentsDisabled) as env:
        off_env.require_enabled()
    assert "ARGUS_CLI_AGENTS_ENABLED" in str(env.value)


async def test_cli_connection_api_answers_disabled_while_switched_off(feat_app, feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    assert (await feat_client.get(f"{API}/cli-agents", headers=admin)).json()["enabled"] is True
    await feat_client.patch(f"{FEATURES}/cli_agents", json={"enabled": False}, headers=admin)
    body = (await feat_client.get(f"{API}/cli-agents", headers=admin)).json()
    assert body["enabled"] is False and all(c["enabled"] is False for c in body["clis"])
    assert body["turned_off"] is True
    r = await feat_client.put(
        f"{API}/cli-agents/claude/key", json={"api_key": "sk-ant-api03-abcdefgh"}, headers=admin
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "cli_agents_disabled"
    assert r.json()["error"]["message"] == ADMIN_OFF
    kinds = (await feat_client.get(f"{API}/providers/kinds", headers=admin)).json()
    assert kinds["cli_providers"] == []
    with pytest.raises(CLIError) as err:
        await feat_app.state.llm.resolve(None, uuid.uuid4(), "claude-cli")
    assert ADMIN_OFF in str(err.value) and err.value.fallback

    await feat_client.patch(f"{FEATURES}/cli_agents", json={"enabled": True}, headers=admin)
    body = (await feat_client.get(f"{API}/cli-agents", headers=admin)).json()
    assert body["enabled"] is True and body["turned_off"] is False


async def test_switched_off_cli_seat_falls_back(feat_app, feat_client):
    admin = await register_and_login(feat_client, "admin@example.com")
    await feat_client.patch(f"{FEATURES}/cli_agents", json={"enabled": False}, headers=admin)
    c = (
        await feat_client.post(
            f"{API}/councils",
            headers=admin,
            json={
                "name": "hybrid",
                "mode": "chat",
                "members": [
                    {
                        "name": "Ada",
                        "role": "assistant",
                        "provider": "claude-cli",
                        "model": "sonnet",
                        "config": {"fallbacks": [{"provider": "mock", "model": "mock-1"}]},
                    },
                    {"name": "Watch", "role": "monitor", "provider": "mock", "model": "mock-1"},
                ],
            },
        )
    ).json()
    r = await feat_client.post(f"{API}/runs", headers=admin, json={"topic": "hello", "council_id": c["id"]})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    for _ in range(400):
        run = (await feat_client.get(f"{API}/runs/{run_id}", headers=admin)).json()
        if run["status"] in ("completed", "failed", "halted", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert run["status"] == "completed", run
    events = (await feat_client.get(f"{API}/runs/{run_id}/events", headers=admin)).json()["events"]
    fallbacks = [e for e in events if e["type"] == "provider.fallback"]
    assert fallbacks, [e["type"] for e in events]
    assert fallbacks[0]["data"]["from"]["provider"] == "claude-cli"
    assert fallbacks[0]["data"]["reason"] == "unavailable"
    assert feat_app.state.cli_agents.transport.calls == []
