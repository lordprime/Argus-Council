"""In-app CLI sign-in through the API: own user only, gates, rate limits, audit without secrets.

The API talks to a real runner (in-process) whose CLIs are the fake login executables from
``test_cli_login_runner``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from argus.db.models import AuditEvent
from argus.main import create_app
from argus.providers.cli.client import LocalRunner, RunnerUnavailable
from tests.conftest import _reset_state, register_and_login
from tests.test_cli_agents_api import _settings
from tests.test_cli_login_runner import _runner

API = "/api/v1"


def _local(tmp_path: Path) -> LocalRunner:
    local = LocalRunner.__new__(LocalRunner)
    local._runner = _runner(tmp_path)
    return local


@pytest.fixture
async def login_app(tmp_path):
    app = create_app(_settings(tmp_path, cli_agents_enabled=True))
    async with app.router.lifespan_context(app):
        local = _local(tmp_path)
        app.state.cli_agents.transport = local
        yield app
        await local._runner.logins.close()
        await _reset_state(app)


@pytest.fixture
async def login_client(login_app):
    async with AsyncClient(transport=ASGITransport(app=login_app), base_url="http://test") as c:
        yield c


async def poll(client, headers, cli: str, until) -> dict:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        st = (await client.get(f"{API}/cli-agents/{cli}/login", headers=headers)).json()
        if until(st):
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(st)


async def test_sign_in_is_for_the_caller_only(login_app, login_client):
    alice = await register_and_login(login_client, "alice@example.com")
    bob = await register_and_login(login_client, "bob@example.com")
    over = (await login_client.get(f"{API}/cli-agents?probe=false", headers=alice)).json()
    flows = {c["cli"]: (c["login_flow"], c["login_methods"]) for c in over["clis"]}
    assert flows["claude"] == ("paste_code", []) and flows["codex"] == ("device_code", [])
    assert "never reads, stores or forwards" in over["notice"]

    r = await login_client.post(f"{API}/cli-agents/claude/login", json={}, headers=alice)
    assert r.status_code == 200, r.text
    st = await poll(login_client, alice, "claude", lambda s: s["needs_code"])
    assert st["verification_url"].startswith("https://claude.com/cai/oauth/authorize")
    assert (await login_client.get(f"{API}/cli-agents/claude/login", headers=bob)).json()["state"] == "none"
    # Bob cannot answer Alice's prompt: his request goes to his own (absent) sign-in.
    r = await login_client.post(
        f"{API}/cli-agents/claude/login/code", json={"code": "good#state"}, headers=bob
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "cli_login_no_prompt"
    r = await login_client.post(
        f"{API}/cli-agents/claude/login/code", json={"code": "good#state"}, headers=alice
    )
    assert r.status_code == 200 and r.json()["state"] == "verifying"
    st = await poll(login_client, alice, "claude", lambda s: s["state"] == "succeeded")
    assert st["error"] is None

    alice_id = (await login_client.get(f"{API}/auth/me", headers=alice)).json()["id"]
    end = time.monotonic() + 5
    rows: list[AuditEvent] = []
    while time.monotonic() < end:
        async with login_app.state.db.sessionmaker() as s:
            rows = list(
                (
                    await s.execute(select(AuditEvent).where(AuditEvent.actor_id == uuid.UUID(alice_id)))
                ).scalars()
            )
        if any("login/code" in r.action for r in rows):
            break
        await asyncio.sleep(0.05)
    login_rows = [r for r in rows if "cli-agents" in r.action]
    assert {r.detail.get("action") for r in login_rows} >= {"login_start", "login_code"}
    assert "good#state" not in str([(r.action, r.detail) for r in rows])


async def test_device_code_flow_and_cancel(login_client):
    alice = await register_and_login(login_client, "alice@example.com")
    await login_client.post(f"{API}/cli-agents/codex/login", json={}, headers=alice)
    st = await poll(login_client, alice, "codex", lambda s: s["user_code"])
    assert (
        st["user_code"] == "8Y19-DW049" and st["verification_url"] == "https://auth.openai.com/codex/device"
    )
    r = await login_client.delete(f"{API}/cli-agents/codex/login", headers=alice)
    assert r.status_code == 200 and r.json()["state"] in ("cancelled", "succeeded")


async def test_codes_with_spaces_are_rejected_before_the_runner(login_client):
    alice = await register_and_login(login_client, "alice@example.com")
    r = await login_client.post(
        f"{API}/cli-agents/claude/login/code", json={"code": "two words"}, headers=alice
    )
    assert r.status_code == 422 and "two words" not in r.text


async def test_start_is_rate_limited(login_client):
    alice = await register_and_login(login_client, "alice@example.com")
    codes = []
    for _ in range(7):
        r = await login_client.post(f"{API}/cli-agents/codex/login", json={}, headers=alice)
        codes.append(r.status_code)
    assert codes[:6] == [200] * 6 and codes[6] == 429


async def test_gates(tmp_path):
    app = create_app(_settings(tmp_path, cli_agents_enabled=True, cli_agents_allow_subscription=False))
    async with app.router.lifespan_context(app):
        app.state.cli_agents.transport = _local(tmp_path)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            alice = await register_and_login(client, "alice@example.com")
            r = await client.post(f"{API}/cli-agents/claude/login", json={}, headers=alice)
            assert r.status_code == 403 and r.json()["error"]["code"] == "cli_subscription_disabled"
            r = await client.post(f"{API}/cli-agents/claude/logout", headers=alice)
            assert r.status_code == 200 and r.json()["ok"] is True  # sign-out stays allowed
        await _reset_state(app)

    (tmp_path / "b").mkdir()
    app = create_app(_settings(tmp_path / "b", cli_agents_enabled=True, cli_agents_allowed=["codex"]))
    async with app.router.lifespan_context(app):
        app.state.cli_agents.transport = _local(tmp_path / "b")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            alice = await register_and_login(client, "alice@example.com")
            r = await client.post(f"{API}/cli-agents/claude/login", json={}, headers=alice)
            assert r.status_code == 403 and r.json()["error"]["code"] == "cli_agents_disabled"
            r = await client.post(f"{API}/cli-agents/nope/login", json={}, headers=alice)
            assert r.status_code == 422
        await _reset_state(app)


async def test_api_keys_cannot_drive_sign_in(login_client):
    alice = await register_and_login(login_client, "alice@example.com")
    r = await login_client.post(f"{API}/auth/api-keys", json={"name": "ci"}, headers=alice)
    assert r.status_code == 201
    kh = {"X-API-Key": r.json()["key"]}
    r = await login_client.post(f"{API}/cli-agents/claude/login", json={}, headers=kh)
    assert r.status_code == 403
    assert (await login_client.get(f"{API}/cli-agents/claude/login", headers=kh)).status_code == 403


async def test_runner_unreachable(login_app, login_client):
    class Down:
        async def login_start(self, *a, **k):
            raise RunnerUnavailable("runner unreachable: ConnectError")

    login_app.state.cli_agents.transport = Down()
    alice = await register_and_login(login_client, "alice@example.com")
    r = await login_client.post(f"{API}/cli-agents/claude/login", json={}, headers=alice)
    assert r.status_code == 503 and "unreachable" in r.json()["error"]["message"]
