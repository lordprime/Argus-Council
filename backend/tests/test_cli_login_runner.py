"""In-app sign-in in the agent runner: each CLI's own login command, with only the link and code
relayed, pasted codes passed through once and never kept, one attempt per user and CLI, deadlines.

The vendor CLIs are replaced by small fake executables that print what the pinned versions print.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from argus.runner.login import LoginError, host_allowed, strip_ansi
from argus.runner.process import Runner, RunnerConfig
from argus.runner.server import create_app
from argus.runner.specs import Binaries

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
CLAUDE_URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc"

FAKE_CLAUDE = f"""
import json, os, sys
argv = sys.argv[1:]
if argv[:2] == ["auth", "logout"]:
    open(os.path.join(os.environ["HOME"], "logged-out"), "w").write("yes")
    print("Successfully logged out"); sys.exit(0)
if argv[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": False}})); sys.exit(1)
assert argv == ["auth", "login", "--claudeai"], argv
assert os.environ.get("BROWSER") == "/bin/true"
print("Opening browser to sign in...")
print("If the browser didn't open, visit: {CLAUDE_URL}")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
code = sys.stdin.readline().strip()
if code == "good#state":
    print("Login successful."); sys.exit(0)
print("Login failed: Request failed with status code 400 for " + code); sys.exit(1)
"""

FAKE_CODEX = """
import sys, time
argv = sys.argv[1:]
if argv == ["logout"]:
    sys.exit(0)
if argv == ["login", "status"]:
    print("Not logged in"); sys.exit(1)
assert argv == ["login", "--device-auth"], argv
print("Follow these steps to sign in with ChatGPT using device code authorization:")
print("1. Open this link in your browser and sign in to your account")
print("   \\x1b[94mhttps://auth.openai.com/codex/device\\x1b[0m")
print("2. Enter this one-time code \\x1b[90m(expires in 15 minutes)\\x1b[0m")
print("   \\x1b[94m8Y19-DW049\\x1b[0m", flush=True)
time.sleep(float(__import__("os").environ.get("FAKE_WAIT", "0.6")))
print("Successfully logged in")
"""

FAKE_GEMINI = """
import json, os, sys, time
if not sys.stdin.isatty():
    print("Manual authorization is required but the current session is non-interactive."); sys.exit(41)
settings = json.load(open(os.path.join(os.environ["HOME"], ".gemini", "settings.json")))
assert settings["security"]["auth"]["selectedType"] == "oauth-personal"
assert settings["ui"]["theme"] == "kept"
assert os.environ["NO_BROWSER"] == "true"
for attempt in range(2):
    print("\\x1b[2J\\x1b[HPlease visit the following URL to authorize the application:")
    print("")
    print("https://accounts.google.com/o/oauth2/v2/auth?state=s%d&client_id=c" % attempt)
    print("")
    code = input("Enter the authorization code: ")
    if code == "good":
        path = os.path.join(os.environ["HOME"], ".gemini", "oauth_creds.json")
        open(path, "w").write(json.dumps({"refresh_token": "1//secret"}))
        time.sleep(60)
    print("Failed to authenticate with authorization code:invalid_grant")
    print("Failed to authenticate with user code. Retrying...")
sys.exit(1)
"""

FAKE_EVIL = """
import sys
print("visit https://claude.com.evil.example/oauth?x=1 or https://evil.example/login")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
sys.stdin.readline()
"""

FAKE_HANG = """
import sys, time
print("Starting...", flush=True)
time.sleep(60)
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _runner(tmp_path: Path, **bins: str) -> Runner:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    defaults = {"claude": FAKE_CLAUDE, "codex": FAKE_CODEX, "gemini": FAKE_GEMINI}
    defaults.update(bins)
    cfg = RunnerConfig(
        home_root=tmp_path / "users",
        binaries=Binaries(**{cli: _exe(d / cli, body) for cli, body in defaults.items()}),
    )
    return Runner(cfg)


@pytest.fixture
async def runner(tmp_path: Path):
    r = _runner(tmp_path)
    yield r
    await r.logins.close()


async def wait_for(runner: Runner, user: str, cli: str, *states: str, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = runner.logins.state(user, cli)
        if st["state"] in states:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"state stayed {runner.logins.state(user, cli)}")


async def wait_needs_code(runner: Runner, user: str, cli: str) -> dict:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        st = runner.logins.state(user, cli)
        if st["needs_code"] or st["state"] not in ("starting", "waiting", "verifying"):
            return st
        await asyncio.sleep(0.05)
    raise AssertionError("never asked for a code")


def test_helpers_strip_ansi_and_check_hosts():
    assert (
        strip_ansi("\x1b[94mhttps://auth.openai.com/codex/device\x1b[0m")
        == "https://auth.openai.com/codex/device"
    )
    hosts = ("claude.com", "anthropic.com")
    assert host_allowed("https://claude.com/cai/oauth", hosts)
    assert host_allowed("https://platform.claude.com/x", hosts)
    assert not host_allowed("https://claude.com.evil.example/x", hosts)
    assert not host_allowed("http://claude.com/x", hosts)
    assert not host_allowed("https://user@claude.com/x", hosts)
    assert not host_allowed("https://claude.com:8443/x", hosts)


async def test_claude_paste_code_flow_succeeds(runner):
    st = await runner.logins.start(USER_A, "claude")
    assert st["flow"] == "paste_code"
    st = await wait_needs_code(runner, USER_A, "claude")
    assert st["state"] == "waiting"
    assert st["verification_url"] == CLAUDE_URL
    assert st["user_code"] is None
    st = await runner.logins.submit_code(USER_A, "claude", " good#state ")
    assert st["state"] == "verifying" and not st["needs_code"]
    st = await wait_for(runner, USER_A, "claude", "succeeded")
    assert st["verification_url"] is None and st["error"] is None
    # another user sees nothing
    assert runner.logins.state(USER_B, "claude")["state"] == "none"


async def test_claude_rejected_code_fails_without_echoing_it(runner):
    await runner.logins.start(USER_A, "claude")
    await wait_needs_code(runner, USER_A, "claude")
    await runner.logins.submit_code(USER_A, "claude", "bad-code-123#zz")
    st = await wait_for(runner, USER_A, "claude", "failed")
    assert st["error"] == "login_failed"
    assert "status code 400" in st["message"]
    assert "bad-code-123#zz" not in json.dumps(st)
    assert runner.logins._attempts[(USER_A, "claude")].secrets_ == []


async def test_codex_device_code_flow(runner):
    await runner.logins.start(USER_A, "codex")
    st = await wait_for(runner, USER_A, "codex", "waiting")
    assert st["verification_url"] == "https://auth.openai.com/codex/device"
    assert st["user_code"] == "8Y19-DW049"
    assert not st["needs_code"]
    st = await wait_for(runner, USER_A, "codex", "succeeded")
    assert st["user_code"] is None
    with pytest.raises(LoginError):
        await runner.logins.submit_code(USER_A, "codex", "anything")


async def test_gemini_runs_in_a_pty_retries_and_stops_once_signed_in(tmp_path):
    runner = _runner(tmp_path)
    home = tmp_path / "users" / USER_A
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(json.dumps({"ui": {"theme": "kept"}}))
    (home / ".gemini" / "oauth_creds.json").write_text("old")
    await runner.logins.start(USER_A, "gemini")
    assert not (home / ".gemini" / "oauth_creds.json").exists()  # a new sign-in replaces the old one
    st = await wait_needs_code(runner, USER_A, "gemini")
    assert st["verification_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?state=s0")
    await runner.logins.submit_code(USER_A, "gemini", "wrong")
    end = time.monotonic() + 10
    while not runner.logins.state(USER_A, "gemini")["retry"]:
        assert time.monotonic() < end
        await asyncio.sleep(0.05)
    st = runner.logins.state(USER_A, "gemini")
    assert st["needs_code"] and "state=s1" in st["verification_url"]
    await runner.logins.submit_code(USER_A, "gemini", "good")
    st = await wait_for(runner, USER_A, "gemini", "succeeded")
    attempt = runner.logins._attempts[(USER_A, "gemini")]
    await asyncio.wait_for(attempt.task, 5)
    assert attempt.proc is not None and attempt.proc.returncode is not None  # stopped, not left running
    assert "1//secret" not in json.dumps(st)


async def test_a_link_off_the_allow_list_is_never_relayed(tmp_path):
    runner = _runner(tmp_path, claude=FAKE_EVIL)
    await runner.logins.start(USER_A, "claude")
    st = await wait_for(runner, USER_A, "claude", "failed")
    assert st["error"] == "unexpected_output"
    assert st["verification_url"] is None
    assert "evil" not in json.dumps(st)


async def test_codes_are_validated_before_reaching_the_cli(runner):
    await runner.logins.start(USER_A, "claude")
    await wait_needs_code(runner, USER_A, "claude")
    for bad in ("two words", "line\nbreak", "", "x" * 513, "semi;colon"):
        with pytest.raises(ValueError):
            await runner.logins.submit_code(USER_A, "claude", bad)
    assert runner.logins.state(USER_A, "claude")["needs_code"]
    await runner.logins.cancel(USER_A, "claude")


async def test_starting_again_cancels_the_previous_attempt(tmp_path):
    runner = _runner(tmp_path, claude=FAKE_HANG)
    await runner.logins.start(USER_A, "claude")
    first = runner.logins._attempts[(USER_A, "claude")]
    await runner.logins.start(USER_A, "claude")
    assert first.state == "cancelled"
    assert first.proc is not None and first.proc.returncode is not None
    st = await runner.logins.cancel(USER_A, "claude")
    assert st["state"] == "cancelled"


async def test_deadline_expires_and_kills_the_cli(tmp_path):
    runner = _runner(tmp_path, claude=FAKE_HANG)
    runner.logins.deadline_s = 0.5
    await runner.logins.start(USER_A, "claude")
    st = await wait_for(runner, USER_A, "claude", "expired")
    assert st["error"] == "timed_out"
    attempt = runner.logins._attempts[(USER_A, "claude")]
    await asyncio.wait_for(attempt.task, 5)
    assert attempt.proc.returncode is not None


async def test_per_user_cap(tmp_path):
    runner = _runner(tmp_path, claude=FAKE_HANG, codex=FAKE_HANG, gemini=FAKE_HANG)
    runner.logins.max_per_user = 2
    await runner.logins.start(USER_A, "claude")
    await runner.logins.start(USER_A, "codex")
    with pytest.raises(LoginError) as exc:
        await runner.logins.start(USER_A, "gemini")
    assert exc.value.code == "busy"
    await runner.logins.start(USER_B, "gemini")  # another user is not affected
    await runner.logins.close()


async def test_finished_attempts_are_forgotten(tmp_path):
    runner = _runner(tmp_path, claude=FAKE_HANG)
    runner.logins.keep_s = 0
    await runner.logins.start(USER_A, "claude")
    await runner.logins.cancel(USER_A, "claude")
    await asyncio.sleep(0.01)
    assert runner.logins.state(USER_A, "claude")["state"] == "none"


async def test_logout_uses_the_cli_or_removes_gemini_files(runner, tmp_path):
    out = await runner.logout(USER_A, "claude")
    assert out["ok"] is True
    assert (tmp_path / "users" / USER_A / "logged-out").exists()
    home = tmp_path / "users" / USER_A / ".gemini"
    home.mkdir(parents=True, exist_ok=True)
    (home / "oauth_creds.json").write_text("{}")
    (home / "google_accounts.json").write_text("{}")
    out = await runner.logout(USER_A, "gemini")
    assert (
        out["ok"]
        and not (home / "oauth_creds.json").exists()
        and not (home / "google_accounts.json").exists()
    )
    assert out["status"]["logged_in"] is None


async def test_not_installed(tmp_path):
    runner = Runner(
        RunnerConfig(home_root=tmp_path / "users", binaries=Binaries(claude=str(tmp_path / "nope")))
    )
    st = await runner.logins.start(USER_A, "claude")
    assert st["state"] == "failed" and st["error"] == "not_installed"


async def test_runner_routes_need_the_token_and_never_echo_codes(tmp_path):
    runner = _runner(tmp_path)
    app = create_app(token="t0k", runner=runner)
    headers = {"Authorization": "Bearer t0k"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runner") as client:
        r = await client.post("/v1/login/start", json={"user_id": USER_A, "cli": "claude"})
        assert r.status_code == 401
        r = await client.post("/v1/login/start", json={"user_id": USER_A, "cli": "claude"}, headers=headers)
        assert r.status_code == 200 and r.json()["state"] in ("starting", "waiting")
        await wait_needs_code(runner, USER_A, "claude")
        r = await client.post(
            "/v1/login/code", json={"user_id": USER_A, "cli": "claude", "code": "has space"}, headers=headers
        )
        assert r.status_code == 422 and "has space" not in r.text
        r = await client.post(
            "/v1/login/code", json={"user_id": USER_A, "cli": "claude", "code": "z" * 700}, headers=headers
        )
        assert r.status_code == 422 and "zzzz" not in r.text
        r = await client.post(
            "/v1/login/start", json={"user_id": USER_A, "cli": "claude", "method": "nope"}, headers=headers
        )
        assert r.status_code == 409 and r.json()["detail"]["error"] == "invalid_method"
        r = await client.post("/v1/login/state", json={"user_id": USER_A, "cli": "claude"}, headers=headers)
        assert r.json()["state"] in ("cancelled", "waiting", "starting")
        r = await client.post("/v1/login/cancel", json={"user_id": USER_A, "cli": "claude"}, headers=headers)
        assert r.status_code == 200
        r = await client.post(
            "/v1/login/state", json={"user_id": "not-a-uuid", "cli": "claude"}, headers=headers
        )
        assert r.status_code == 422
    await runner.logins.close()
