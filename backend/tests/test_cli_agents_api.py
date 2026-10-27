"""CLI agents through the API: opt-in gate, per-user keys and sessions, hybrid fallback, export."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile

import orjson
import pytest
from httpx import ASGITransport, AsyncClient

from argus.core.config import Settings
from argus.db.models import CliSession
from argus.main import create_app
from argus.providers.cli.errors import CLIError
from argus.providers.types import CompletionRequest, Message
from argus.runner.specs import InvokeSpec
from tests.conftest import _reset_state, register_and_login

API = "/api/v1"


class FakeRunner:
    """Stands in for the runner service: every CLI turn hits a usage limit."""

    def __init__(self) -> None:
        self.calls: list[InvokeSpec] = []
        self.purged: list[str] = []
        self.homes: dict[str, set[tuple[str, str]]] = {}

    async def invoke(self, spec: InvokeSpec):
        self.calls.append(spec)
        yield {"type": "system", "subtype": "init", "session_id": spec.session_id}
        yield {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "Claude usage limit reached."}],
            },
            "error": "rate_limit",
            "is_api_error_message": True,
            "api_error_status": 429,
        }
        yield {
            "type": "result",
            "is_error": True,
            "result": "Claude usage limit reached.",
            "api_error_status": 429,
        }
        yield {"type": "argus.exit", "ok": False, "exit_code": 1, "error": "exit_status", "detail": ""}

    async def versions(self, refresh: bool = False):
        return {"claude": "2.1.308 (Claude Code)", "codex": "codex-cli 0.163.0", "gemini": "0.65.0"}

    async def status(self, user_id: str, cli: str):
        return {
            "logged_in": cli == "claude",
            "method": "claude.ai" if cli == "claude" else None,
            "verified": True,
        }

    async def has_session(self, user_id: str, cli: str, session_id: str) -> bool:
        return (cli, session_id) in self.homes.get(user_id, set())

    async def sessions(self, user_id: str, cli: str):
        return []

    async def purge(self, user_id: str) -> bool:
        self.purged.append(user_id)
        return True


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/argus.db",
        secret_key="test-secret-key-that-is-long-enough-1234567890",
        rate_limit_per_minute=10_000,
        data_dir=str(tmp_path / "data"),
        **kw,
    )


@pytest.fixture
async def cli_app(tmp_path):
    app = create_app(_settings(tmp_path, cli_agents_enabled=True))
    async with app.router.lifespan_context(app):
        app.state.cli_agents.transport = FakeRunner()
        yield app
        await _reset_state(app)


@pytest.fixture
async def cli_client(cli_app):
    async with AsyncClient(transport=ASGITransport(app=cli_app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------- disabled by default
async def test_cli_agents_are_disabled_by_default(app, client, auth):
    assert Settings().cli_agents_enabled is False
    r = await client.get(f"{API}/cli-agents", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and "terms" in body["notice"]
    assert all(c["enabled"] is False for c in body["clis"])
    r = await client.put(
        f"{API}/cli-agents/claude/key", json={"api_key": "sk-ant-api03-abcdefgh"}, headers=auth
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "cli_agents_disabled"
    kinds = (await client.get(f"{API}/providers/kinds", headers=auth)).json()
    assert kinds["cli_providers"] == []
    with pytest.raises(CLIError) as err:
        await app.state.llm.resolve(None, uuid.uuid4(), "claude-cli")
    assert err.value.fallback  # a seat with a fallback route still runs


async def test_cli_provider_names_are_reserved(client, auth):
    r = await client.post(f"{API}/providers", json={"name": "claude-cli", "kind": "anthropic"}, headers=auth)
    assert r.status_code == 409


# ---------------------------------------------------------------- enabled
async def test_status_keys_are_per_user_and_never_returned(cli_app, cli_client):
    alice = await register_and_login(cli_client, "alice@example.com")
    bob = await register_and_login(cli_client, "bob@example.com")
    r = await cli_client.put(
        f"{API}/cli-agents/claude/key", json={"api_key": "sk-ant-api03-alicesecretkey"}, headers=alice
    )
    assert r.status_code == 200 and r.json() == {"cli": "claude", "has_api_key": True, "key_hint": "sk-…tkey"}
    over = (await cli_client.get(f"{API}/cli-agents", headers=alice)).json()
    assert over["enabled"] is True and "alicesecretkey" not in str(over)
    claude = next(c for c in over["clis"] if c["cli"] == "claude")
    assert claude["has_api_key"] and claude["version"].startswith("2.1.308") and claude["logged_in"] is True
    assert claude["login_command"] == "./scripts/agent-login.sh <your-email> claude"
    bob_view = (await cli_client.get(f"{API}/cli-agents", headers=bob)).json()
    assert not next(c for c in bob_view["clis"] if c["cli"] == "claude")["has_api_key"]

    svc = cli_app.state.cli_agents
    alice_id = uuid.UUID((await cli_client.get(f"{API}/auth/me", headers=alice)).json()["id"])
    bob_id = uuid.UUID((await cli_client.get(f"{API}/auth/me", headers=bob)).json()["id"])
    assert await svc.api_key(alice_id, "claude") == "sk-ant-api03-alicesecretkey"
    assert await svc.api_key(bob_id, "claude") is None
    # Providers are per user: Bob's claude-cli never carries Alice's identity.
    assert svc.provider_for(bob_id, "claude-cli").owner_id == bob_id
    assert svc.provider_for(alice_id, "claude-cli") is not svc.provider_for(bob_id, "claude-cli")

    assert (await cli_client.delete(f"{API}/cli-agents/claude/key", headers=bob)).status_code == 404
    assert (await cli_client.delete(f"{API}/cli-agents/claude/key", headers=alice)).status_code == 204
    kinds = (await cli_client.get(f"{API}/providers/kinds", headers=alice)).json()
    assert kinds["cli_providers"] == ["claude-cli", "codex-cli", "gemini-cli"]


async def test_sessions_are_private_to_their_owner(cli_app, cli_client):
    alice = await register_and_login(cli_client, "alice@example.com")
    bob = await register_and_login(cli_client, "bob@example.com")
    alice_id = uuid.UUID((await cli_client.get(f"{API}/auth/me", headers=alice)).json()["id"])
    bob_id = uuid.UUID((await cli_client.get(f"{API}/auth/me", headers=bob)).json()["id"])
    svc = cli_app.state.cli_agents
    sid = str(uuid.uuid4())
    await svc.remember_session(alice_id, "claude", sid, None, "seat-1")
    cli_app.state.cli_agents.transport.homes[str(alice_id)] = {("claude", sid)}

    mine = (await cli_client.get(f"{API}/cli-agents/sessions", headers=alice)).json()
    assert [s["session_id"] for s in mine] == [sid]
    assert (await cli_client.get(f"{API}/cli-agents/sessions", headers=bob)).json() == []
    row_id = mine[0]["id"]
    r = await cli_client.patch(
        f"{API}/cli-agents/sessions/{row_id}", json={"label": "lit review"}, headers=bob
    )
    assert r.status_code == 404
    r = await cli_client.patch(
        f"{API}/cli-agents/sessions/{row_id}", json={"label": "lit review"}, headers=alice
    )
    assert r.status_code == 200 and r.json()["label"] == "lit review"

    # Bob cannot pin Alice's session on a seat of his, even knowing its id.
    assert await svc.session_owned(alice_id, "claude", sid)
    assert not await svc.session_owned(bob_id, "claude", sid)
    with pytest.raises(CLIError) as err:
        await svc.provider_for(bob_id, "claude-cli").complete(
            CompletionRequest(
                model="sonnet",
                messages=[Message.user("hi")],
                metadata={"run_id": "r", "member_id": "m", "cli_session": sid},
            )
        )
    assert err.value.category == "session"
    assert cli_app.state.cli_agents.transport.calls == []


async def test_admin_cannot_use_another_users_cli(cli_app, cli_client):
    admin = await register_and_login(cli_client, "admin@example.com")  # first account: admin
    member = await register_and_login(cli_client, "member@example.com")
    await cli_client.put(
        f"{API}/cli-agents/codex/key", json={"api_key": "sk-proj-membersecret1"}, headers=member
    )
    # No route takes a user id: the admin only ever sees their own state.
    view = (await cli_client.get(f"{API}/cli-agents", headers=admin)).json()
    assert not next(c for c in view["clis"] if c["cli"] == "codex")["has_api_key"]
    r = await cli_client.get(f"{API}/cli-agents/sessions", headers=admin, params={"user_id": "x"})
    assert r.status_code == 200 and r.json() == []


async def _wait(client, auth, run_id, timeout=20):
    for _ in range(int(timeout / 0.05)):
        r = (await client.get(f"{API}/runs/{run_id}", headers=auth)).json()
        if r["status"] in ("completed", "failed", "halted", "cancelled"):
            return r
        await asyncio.sleep(0.05)
    raise AssertionError("run did not finish")


async def test_hybrid_seat_falls_back_from_cli_to_api_provider(cli_app, cli_client):
    auth = await register_and_login(cli_client, "owner@example.com")
    c = (
        await cli_client.post(
            f"{API}/councils",
            headers=auth,
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
    r = await cli_client.post(f"{API}/runs", headers=auth, json={"topic": "hello", "council_id": c["id"]})
    assert r.status_code == 201, r.text
    run = await _wait(cli_client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    events = (await cli_client.get(f"{API}/runs/{run['id']}/events", headers=auth)).json()["events"]
    fallbacks = [e for e in events if e["type"] == "provider.fallback"]
    assert fallbacks, [e["type"] for e in events]
    data = fallbacks[0]["data"]
    assert data["from"]["provider"] == "claude-cli" and data["to"]["provider"] == "mock"
    assert data["reason"] in ("rate_limit", "quota")
    thoughts = [e for e in events if e["type"] == "thought.completed" and e["actor"]["name"] == "Ada"]
    assert thoughts and all(t["data"]["provider"] == "mock" for t in thoughts)
    calls = cli_app.state.cli_agents.transport.calls
    owner_id = (await cli_client.get(f"{API}/auth/me", headers=auth)).json()["id"]
    # The CLI ran for the run's owner only, with every turn tied to the seat, and was not retried in place.
    assert calls and all(s.user_id == owner_id and s.persist for s in calls)
    assert len(calls) == len(fallbacks)


async def test_export_never_contains_cli_credentials(cli_app, cli_client):
    auth = await register_and_login(cli_client, "owner@example.com")
    await cli_client.put(
        f"{API}/cli-agents/gemini/key", json={"api_key": "AIzaSyExportLeakCheck123456"}, headers=auth
    )
    uid = uuid.UUID((await cli_client.get(f"{API}/auth/me", headers=auth)).json()["id"])
    async with cli_app.state.db.sessionmaker() as s:
        s.add(CliSession(owner_id=uid, cli="gemini", session_id=str(uuid.uuid4()), label=""))
        await s.commit()
    r = await cli_client.post(f"{API}/me/export", headers=auth)
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert "cli_credentials.json" not in names
    everything = b"".join(zf.read(n) for n in names)
    assert b"AIzaSyExportLeakCheck" not in everything and b"api_key_enc" not in everything
    assert len(orjson.loads(zf.read("cli_sessions.json"))) == 1


async def test_account_deletion_purges_runner_home(cli_app, cli_client):
    await register_and_login(cli_client, "admin@example.com")
    member = await register_and_login(cli_client, "leaver@example.com")
    uid = (await cli_client.get(f"{API}/auth/me", headers=member)).json()["id"]
    r = await cli_client.request(
        "DELETE", f"{API}/me", json={"password": "correct horse battery staple"}, headers=member
    )
    assert r.status_code == 204, r.text
    assert cli_app.state.cli_agents.transport.purged == [uid]
