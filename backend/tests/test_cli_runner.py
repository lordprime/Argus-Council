"""Agent runner: per-user HOME isolation, session allow-list, secrets hygiene and API auth.

The vendor CLIs are replaced by small fake executables that echo what they were given.
"""

from __future__ import annotations

import json
import stat
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from argus.runner.process import Runner, RunnerConfig, redact
from argus.runner.server import create_app
from argus.runner.specs import Binaries, InvokeSpec

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"

FAKE_CLAUDE = """
import json, os, sys
stdin = sys.stdin.read()
argv = sys.argv[1:]
sid = None
for flag in ("--session-id", "--resume"):
    if flag in argv:
        sid = argv[argv.index(flag) + 1]
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "home": os.environ["HOME"],
                  "cwd": os.getcwd(), "argv": argv, "stdin": stdin, "env_keys": sorted(os.environ)}))
print("a banner line that is not json")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": sid}))
sys.stderr.write("debug: using key " + os.environ.get("ANTHROPIC_API_KEY", "none") + "\\n")
"""

FAKE_CODEX = """
import json, sys, uuid
sys.stdin.read()
argv = sys.argv[1:]
tid = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())
print(json.dumps({"type": "thread.started", "thread_id": tid}))
print(json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "hi"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}}))
"""

FAKE_HANG = """
import time
time.sleep(60)
"""

FAKE_VERSION = """
import sys
print("9.9.9 (fake)")
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    bins = tmp_path / "bin"
    bins.mkdir()
    cfg = RunnerConfig(
        home_root=tmp_path / "users",
        binaries=Binaries(
            claude=_exe(bins / "claude", FAKE_CLAUDE),
            codex=_exe(bins / "codex", FAKE_CODEX),
            gemini=_exe(bins / "gemini", FAKE_HANG),
        ),
    )
    return Runner(cfg)


async def collect(runner: Runner, **kw) -> list[dict]:
    return [e async for e in runner.invoke(InvokeSpec(**kw))]


async def test_cli_runs_in_the_users_own_home_with_a_clean_env(runner, monkeypatch):
    monkeypatch.setenv("ARGUS_RUNNER_TOKEN", "runner-secret-token")
    monkeypatch.setenv("ARGUS_SECRET_KEY", "api-secret")
    sid = str(uuid.uuid4())
    evs = await collect(
        runner,
        user_id=USER_A,
        cli="claude",
        prompt="the prompt",
        session_id=sid,
        run_key="run1-seat1",
        api_key="sk-ant-api03-supersecretvalue",
    )
    init, result, exit_ = evs
    home = runner.cfg.home_root / USER_A
    assert init["home"] == str(home)
    assert init["cwd"] == str(home / "work" / "run1-seat1")
    assert init["stdin"] == "the prompt" and "the prompt" not in init["argv"]
    # Nothing from the runner's own environment leaks in, only the user's key.
    assert "ARGUS_RUNNER_TOKEN" not in init["env_keys"] and "ARGUS_SECRET_KEY" not in init["env_keys"]
    assert "ANTHROPIC_API_KEY" in init["env_keys"]
    assert result["type"] == "result"
    assert exit_["type"] == "argus.exit" and exit_["ok"] and exit_["session_id"] == sid
    # stderr comes back redacted
    assert "supersecretvalue" not in exit_["detail"] and "[REDACTED]" in exit_["detail"]
    assert oct(home.stat().st_mode & 0o777) == "0o700"


async def test_resume_is_limited_to_the_users_own_sessions(runner):
    sid = str(uuid.uuid4())
    await collect(runner, user_id=USER_A, cli="claude", prompt="one", session_id=sid, run_key="r1")
    # User B cannot resume A's session, even knowing its id.
    stolen = await collect(runner, user_id=USER_B, cli="claude", prompt="x", session_id=sid, resume=True)
    assert stolen == [
        {
            "type": "argus.exit",
            "ok": False,
            "error": "unknown_session",
            "detail": "No such session for this user and CLI.",
        }
    ]
    # Nor can a session be resumed under another CLI.
    assert (await collect(runner, user_id=USER_A, cli="codex", prompt="x", session_id=sid, resume=True))[0][
        "error"
    ] == "unknown_session"
    # A can, in the same working directory it started in.
    evs = await collect(runner, user_id=USER_A, cli="claude", prompt="two", session_id=sid, resume=True)
    assert evs[0]["cwd"].endswith("/work/r1") and "--resume" in evs[0]["argv"]
    assert runner.has_session(USER_A, "claude", sid) and not runner.has_session(USER_B, "claude", sid)
    assert [s["session_id"] for s in runner.sessions(USER_A, "claude")] == [sid]


async def test_codex_session_id_is_read_from_thread_started(runner):
    evs = await collect(runner, user_id=USER_A, cli="codex", prompt="hi", run_key="r2")
    tid = evs[0]["thread_id"]
    assert evs[-1]["session_id"] == tid and runner.has_session(USER_A, "codex", tid)
    again = await collect(runner, user_id=USER_A, cli="codex", prompt="more", session_id=tid, resume=True)
    assert again[0]["thread_id"] == tid


async def test_one_shot_is_not_recorded(runner):
    evs = await collect(runner, user_id=USER_A, cli="claude", prompt="hi", persist=False)
    assert evs[-1]["ok"] and evs[-1]["session_id"] is None
    assert runner.sessions(USER_A, "claude") == []
    assert "--no-session-persistence" in evs[0]["argv"]


async def test_timeout_kills_the_process(runner):
    evs = await collect(runner, user_id=USER_A, cli="gemini", prompt="hi", timeout_s=5)
    assert evs[-1]["error"] == "timeout" and not evs[-1]["ok"]


async def test_missing_binary_is_reported(tmp_path):
    runner = Runner(RunnerConfig(home_root=tmp_path, binaries=Binaries(claude=str(tmp_path / "nope"))))
    evs = await collect(runner, user_id=USER_A, cli="claude", prompt="hi")
    assert evs[-1]["error"] == "not_installed"


async def test_purge_removes_only_that_users_home(runner):
    await collect(runner, user_id=USER_A, cli="claude", prompt="a", persist=False)
    await collect(runner, user_id=USER_B, cli="claude", prompt="b", persist=False)
    assert await runner.purge(USER_A)
    assert not (runner.cfg.home_root / USER_A).exists() and (runner.cfg.home_root / USER_B).exists()


def test_redact_masks_keys_and_tokens():
    text = (
        "Incorrect API key provided: sk-invalid123. bearer abcdefghijk AIzaSyA1234567890123456789012 ya29.tok"
    )
    out = redact(text, ["literal-secret"])
    for leaked in ("sk-invalid123", "abcdefghijk", "AIzaSyA", "ya29.tok"):
        assert leaked not in out


# ---------------------------------------------------------------- HTTP API
@pytest.fixture
def runner_app(runner):
    return create_app(token="runner-token-for-tests", runner=runner)


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://runner")


async def test_runner_api_requires_the_shared_token(runner_app):
    async with await _client(runner_app) as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/v1/clis")).status_code == 401
        assert (await c.get("/v1/clis", headers={"Authorization": "Bearer wrong"})).status_code == 401
        body = {"user_id": USER_A, "cli": "claude", "prompt": "hi"}
        assert (await c.post("/v1/invoke", json=body)).status_code == 401
        assert (await c.delete(f"/v1/users/{USER_A}")).status_code == 401


async def test_runner_without_a_token_refuses_everything(runner):
    app = create_app(token="", runner=runner)
    async with await _client(app) as c:
        r = await c.post(
            "/v1/status", json={"user_id": USER_A, "cli": "claude"}, headers={"Authorization": "Bearer "}
        )
        assert r.status_code == 401


async def test_runner_invoke_streams_ndjson_and_never_echoes_bad_bodies(runner_app):
    auth = {"Authorization": "Bearer runner-token-for-tests"}
    async with await _client(runner_app) as c:
        r = await c.post(
            "/v1/invoke",
            json={"user_id": USER_A, "cli": "claude", "prompt": "hi", "persist": False},
            headers=auth,
        )
        assert r.status_code == 200
        lines = [json.loads(line) for line in r.text.splitlines() if line]
        assert lines[-1]["type"] == "argus.exit" and lines[-1]["ok"]
        bad = await c.post(
            "/v1/invoke",
            json={
                "user_id": "../../x",
                "cli": "claude",
                "prompt": "TOPSECRETPROMPT",
                "api_key": "sk-ant-leakme1234",
            },
            headers=auth,
        )
        assert bad.status_code == 422
        assert "TOPSECRETPROMPT" not in bad.text and "leakme" not in bad.text
        assert (await c.get("/v1/users/not-a-uuid/sessions/claude", headers=auth)).status_code == 422
