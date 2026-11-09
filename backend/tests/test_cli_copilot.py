"""GitHub Copilot CLI (``copilot`` 1.0.108) as a council seat: argv, key env, the JSONL parser, the
usage file, the device-code sign-in, sign-out and status, and full turns through the runner against a
fake ``copilot`` executable that prints what the pinned version prints.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
import uuid
from pathlib import Path

import pytest

from argus.providers.cli.errors import FALLBACK_CATEGORIES
from argus.providers.cli.parsers import make_parser
from argus.providers.cli.vendors.copilot import Parser
from argus.runner.clis import key_env, login_spec_for, logout_spec_for, plugin
from argus.runner.clis.copilot import session_from_event, tools_reported
from argus.runner.process import Runner, RunnerConfig
from argus.runner.specs import Binaries, InvokeSpec, TurnContext, build_command

USER_A = "11111111-1111-4111-8111-111111111111"
SID = "3f2a7c1e-9b4d-4e8f-a1c2-5d6e7f8a9b0c"
TOOLS_OFF = (
    "--output-format",
    "json",
    "--no-custom-instructions",
    "--no-ask-user",
    "--disable-builtin-mcps",
    "--disallow-temp-dir",
    "--available-tools=",
    "--deny-tool=shell",
    "--deny-tool=write",
    "--deny-tool=read",
    "--deny-tool=url",
    "--deny-tool=memory",
    "--sync-session=off",
    "--no-auto-update",
)
FORBIDDEN_PREFIXES = (
    "--allow-all",
    "--yolo",
    "--allow-tool",
    "--allow-url",
    "--autopilot",
    "--trust-folder",
    "--enable-memory",
    "--add-dir",
    "--plugin-dir",
    "--additional-mcp-config",
    "--enable-mcp-server",
    "--excluded-tools",
    "--mode",
    "--agent",
    "-p",
    "--prompt",
)


def turn(tmp_path: Path) -> TurnContext:
    return TurnContext(
        home=str(tmp_path),
        cwd=str(tmp_path / "w"),
        turn_dir=str(tmp_path / "t"),
        system_prompt_file=str(tmp_path / "p.md"),
    )


def build(tmp_path: Path, **kw):
    kw.setdefault("prompt", "hello")
    spec = InvokeSpec(user_id=USER_A, cli="copilot", **kw)
    return build_command(spec, binaries=Binaries(), system_prompt_file=None, turn=turn(tmp_path))


def assert_safe(argv: list[str]) -> None:
    for flag in TOOLS_OFF:
        assert flag in argv, flag
    for arg in argv:
        assert not any(
            arg == p or arg.startswith(p + "=") or arg.startswith(p + "-") for p in FORBIDDEN_PREFIXES
        ), arg


# ----------------------------------------------------------------------------------------- argv


def test_new_session_uses_the_id_argus_chose_and_the_prompt_comes_on_stdin(tmp_path):
    cmd = build(tmp_path, session_id=SID)
    assert cmd.argv[0] == "copilot"
    assert_safe(cmd.argv)
    assert cmd.argv[-2:] == ["--session-id", SID]
    assert cmd.stdin == "hello" and "hello" not in cmd.argv
    usage = cmd.argv[cmd.argv.index("--usage-output-file") + 1]
    assert usage == str(tmp_path / "t" / "copilot-usage.json") and cmd.result_file == usage
    assert cmd.env == {
        "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_PLUGIN_DIR_ONLY": "true",
        "COPILOT_DISABLE_LOGIN_SHELL_ENV": "1",
        "COPILOT_DISABLE_RESUME_AUTO_CD": "1",
        "COPILOT_TELEMETRY_DISABLED": "true",
    }
    assert cmd.files == {} and cmd.protocol == "stdio"


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"session_id": SID},
        {"session_id": SID, "resume": True},
        {"persist": False},
        {"model": "claude-sonnet-5.5", "effort": "max", "api_key": "github_pat_x"},
    ],
)
def test_tools_off_flags_always_present_and_forbidden_flags_never(tmp_path, kw):
    assert_safe(build(tmp_path, **kw).argv)


def test_resume_uses_the_same_flag_and_skips_the_system_prompt(tmp_path):
    cmd = build(tmp_path, session_id=SID, resume=True, system_prompt="be terse")
    assert cmd.argv[-2:] == ["--session-id", SID]
    assert cmd.stdin == "hello"


def test_system_prompt_is_folded_into_a_new_session_prompt(tmp_path):
    cmd = build(tmp_path, session_id=SID, system_prompt="be terse")
    assert cmd.stdin == "<system_instructions>\nbe terse\n</system_instructions>\n\nhello"
    assert "be terse" not in " ".join(cmd.argv)
    assert not any(a.startswith(("--system", "--agent")) for a in cmd.argv)


def test_one_shot_and_no_session_never_name_an_id(tmp_path):
    assert "--session-id" not in build(tmp_path, persist=False).argv
    assert "--session-id" not in build(tmp_path, persist=False, session_id=SID).argv
    assert "--session-id" not in build(tmp_path).argv


def test_model_and_effort(tmp_path):
    argv = build(tmp_path, model="gpt-6.1-sol", effort="xhigh").argv
    assert argv[argv.index("--model") + 1] == "gpt-6.1-sol"
    assert argv[argv.index("--reasoning-effort") + 1] == "xhigh"
    argv = build(tmp_path, model="default").argv
    assert "--model" not in argv and "--reasoning-effort" not in argv


def test_bad_values_never_reach_argv(tmp_path):
    for kw in ({"model": "--yolo"}, {"model": "a b"}, {"effort": "turbo"}, {"session_id": "../x"}):
        with pytest.raises(ValueError):
            InvokeSpec(user_id=USER_A, cli="copilot", prompt="x", **kw)


def test_api_key_goes_to_copilot_github_token_only(tmp_path):
    cmd = build(tmp_path, api_key="github_pat_secret123")
    assert cmd.env["COPILOT_GITHUB_TOKEN"] == "github_pat_secret123"
    assert "GH_TOKEN" not in cmd.env and "GITHUB_TOKEN" not in cmd.env
    assert not any("github_pat_secret123" in a for a in cmd.argv)
    assert key_env("copilot", None, "k", None) == {"COPILOT_GITHUB_TOKEN": "k"}
    assert "COPILOT_GITHUB_TOKEN" not in build(tmp_path).env


def test_plugin_metadata():
    p = plugin("copilot")
    assert p.label == "GitHub Copilot CLI"
    assert p.key_env == "COPILOT_GITHUB_TOKEN" and not p.key_providers
    assert p.supports_effort and p.usage_is_session_total
    assert p.suggested_models[0] == "default"
    spec = login_spec_for("copilot")
    assert spec is not None and spec.flow == "device_code" and not spec.pty
    assert spec.argv(Binaries()) == ["copilot", "login", "--device-code"]
    assert spec.url_hosts == ("github.com",)
    assert spec.settings == (".copilot/settings.json", {"storeTokenPlaintext": True})
    assert logout_spec_for("copilot") == (None, (".copilot/config.json",))
    assert p.status_files == (".copilot/config.json",)


# --------------------------------------------------------------------------------- runner hooks


def test_session_read_back_from_start_and_result():
    assert session_from_event({"type": "session.start", "data": {"sessionId": SID.upper()}}) == SID
    assert session_from_event({"type": "result", "sessionId": SID, "exitCode": 0}) == SID
    assert session_from_event({"type": "session.start", "data": {"sessionId": "../etc"}}) is None
    assert session_from_event({"type": "assistant.message", "data": {"sessionId": SID}}) is None


def test_tools_reported():
    start = {"type": "session.start", "data": {"sessionId": SID, "selectedModel": "m"}}
    assert tools_reported(start) is None  # 1.0.108 lists no tools at start
    assert tools_reported({"type": "session.tools_updated", "data": {"model": "m"}}) is None
    assert tools_reported({"type": "session.start", "data": {"tools": []}}) == []
    assert tools_reported({"type": "session.tools_updated", "data": {"tools": ["bash", "view"]}}) == [
        "bash",
        "view",
    ]
    ran = {"type": "tool.execution_start", "data": {"toolCallId": "c", "toolName": "bash"}}
    assert tools_reported(ran) == ["bash"]
    mcp = {"type": "session.mcp_server_tools_loaded", "data": {"serverName": "github", "toolCount": 12}}
    assert tools_reported(mcp) == ["mcp:github"]
    empty = {"type": "session.mcp_server_tools_loaded", "data": {"serverName": "x", "toolCount": 0}}
    assert tools_reported(empty) is None
    assert tools_reported({"type": "assistant.message_delta", "data": {"deltaContent": "x"}}) is None


# ---------------------------------------------------------------------------------------- parser


def ev(kind: str, **data) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": "2026-11-10T00:00:00Z",
        "parentId": None,
        "ephemeral": kind.endswith("_delta"),
        "type": kind,
        "data": data,
    }


USAGE_FILE = {
    "totalPremiumRequestCost": 1,
    "totalUserRequests": 1,
    "totalApiDurationMs": 900,
    "sessionStartTime": "2026-11-10T00:00:00Z",
    "codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": []},
    "totalNanoAiu": 120000000,
    "modelMetrics": {
        "claude-sonnet-5.5": {
            "requests": {"count": 1, "cost": 1},
            "usage": {
                "inputTokens": 1200,
                "outputTokens": 40,
                "cacheReadTokens": 1000,
                "cacheWriteTokens": 50,
            },
        }
    },
    "currentModel": "claude-sonnet-5.5",
    "lastCallInputTokens": 1200,
    "lastCallOutputTokens": 40,
}


def run(events: list[dict], exit_record: dict | None = None):
    parser = Parser()
    out = []
    for e in events:
        out += parser.feed(e)
    return out, parser.finish(exit_record or {"type": "argus.exit", "ok": True, "session_id": None})


def test_make_parser_loads_the_copilot_parser():
    assert isinstance(make_parser("copilot"), Parser)


def test_text_thinking_usage_and_session_without_duplicates():
    events = [
        ev("session.start", sessionId=SID, selectedModel="claude-sonnet-5.5", copilotVersion="1.0.108"),
        ev("assistant.reasoning_delta", reasoningId="r1", deltaContent="Think"),
        ev("assistant.reasoning_delta", reasoningId="r1", deltaContent="ing."),
        ev("assistant.reasoning", reasoningId="r1", content="Thinking."),
        ev("assistant.message_delta", messageId="m1", deltaContent="Hello"),
        ev("assistant.message_delta", messageId="m1", deltaContent=" world"),
        ev(
            "assistant.message",
            messageId="m1",
            content="Hello world",
            model="claude-sonnet-5.5",
            toolRequests=[],
            reasoningText="Thinking.",
            outputTokens=40,
        ),
        ev("assistant.turn_end", turnId="0", model="claude-sonnet-5.5"),
        {
            "type": "result",
            "timestamp": "t",
            "sessionId": SID,
            "exitCode": 0,
            "usage": {"premiumRequests": 1, "totalApiDurationMs": 900, "sessionDurationMs": 1000},
        },
        {"type": "argus.result_file", "data": USAGE_FILE},
    ]
    out, res = run(events)
    assert [(e.type, e.text) for e in out] == [
        ("thinking", "Think"),
        ("thinking", "ing."),
        ("text", "Hello"),
        ("text", " world"),
    ]
    assert res.text == "Hello world" and res.thinking == "Thinking."
    assert res.session_id == SID and res.model == "claude-sonnet-5.5"
    assert (res.usage.input_tokens, res.usage.output_tokens) == (150, 40)
    assert (res.usage.cache_read_tokens, res.usage.cache_write_tokens) == (1000, 50)
    assert res.reported_cost_usd is None and res.failure is None


def test_a_message_without_deltas_is_used_once_and_messages_are_separated():
    events = [
        ev("assistant.message", messageId="m1", content="First."),
        ev("assistant.message_delta", messageId="m2", deltaContent="Second"),
        ev("assistant.message", messageId="m2", content="Second"),
    ]
    out, res = run(events)
    assert [e.text for e in out] == ["First.", "\n\nSecond"]
    assert res.text == "First.\n\nSecond"


def test_subagent_events_are_ignored():
    events = [
        ev("assistant.message_delta", messageId="m1", deltaContent="sub", parentToolCallId="c1"),
        {**ev("assistant.message", messageId="m9", content="agent"), "agentId": "a1"},
        ev("assistant.message", messageId="m2", content="main"),
    ]
    _, res = run(events)
    assert res.text == "main"


def test_usage_falls_back_to_token_details():
    data = {
        "tokenDetails": {
            "input": {"tokenCount": 7},
            "output": {"tokenCount": 3},
            "cache_read": {"tokenCount": 100},
            "cache_write": {"tokenCount": 2},
        }
    }
    _, res = run([{"type": "argus.result_file", "data": data}])
    u = res.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (7, 3, 100, 2)
    _, res = run([{"type": "argus.result_file", "data": ["not", "a", "dict"]}])
    assert res.usage.input_tokens == 0


@pytest.mark.parametrize(
    ("data", "category"),
    [
        (
            {"errorType": "rate_limit", "statusCode": 429, "message": "You've hit your session rate limit."},
            "rate_limit",
        ),
        ({"errorType": "session", "message": "You've hit the rate limit for this model."}, "rate_limit"),
        ({"errorType": "session", "errorCode": "user_weekly_rate_limited", "message": "x"}, "rate_limit"),
        (
            {
                "errorType": "session",
                "statusCode": 429,
                "errorCode": "quota_exceeded",
                "message": "You have exceeded your premium requests quota.",
            },
            "quota",
        ),
        ({"errorType": "session", "statusCode": 402, "message": "Plan: 100% used, resets Dec 1."}, "quota"),
        ({"errorType": "session", "errorCode": "free_quota_exceeded", "message": "limit"}, "quota"),
        ({"errorType": "authentication", "statusCode": 401, "message": "Bad credentials"}, "auth"),
        ({"errorType": "session", "statusCode": 403, "message": "Forbidden"}, "auth"),
        ({"errorType": "session", "message": "Error: No authentication information found."}, "auth"),
        ({"errorType": "session", "statusCode": 503, "message": "Service Unavailable"}, "unavailable"),
        ({"errorType": "session", "statusCode": 400, "message": "Model gpt-x is not supported"}, "invalid"),
    ],
)
def test_session_errors_map_to_fallback_categories(data, category):
    _, res = run([ev("session.error", **data)], {"type": "argus.exit", "ok": False, "error": "exit_status"})
    assert res.failure is not None and res.failure.category == category


def test_a_failed_model_call_decides_the_category_of_a_failed_turn():
    failure = ev(
        "model.call_failure",
        source="top_level",
        statusCode=429,
        errorMessage="Too Many Requests",
        quotaSnapshots={},
    )
    _, res = run([failure], {"type": "argus.exit", "ok": False, "error": "exit_status", "detail": "boom"})
    assert res.failure.category == "rate_limit"
    # a generic session.error after it takes the call's category
    _, res = run([failure, ev("session.error", errorType="execution", message="Execution failed")])
    assert res.failure.category == "rate_limit"
    # a retried call that then succeeds is not a failure
    _, res = run([failure, ev("assistant.message", messageId="m", content="ok")])
    assert res.failure is None and res.text == "ok"
    # Argus stopping the turn keeps its own category
    _, res = run([failure], {"type": "argus.exit", "ok": False, "error": "tools_not_disabled", "detail": "t"})
    assert res.failure.category not in FALLBACK_CATEGORIES


def test_auth_errors_on_stderr_fall_back():
    exit_ = {
        "type": "argus.exit",
        "ok": False,
        "error": "exit_status",
        "exit_code": 1,
        "detail": "Error: Authentication token found but could not be validated.",
    }
    _, res = run([], exit_)
    assert res.failure.category == "auth"


def test_warnings_are_kept_and_first_failure_wins():
    events = [
        ev(
            "session.warning",
            warningType="subscription",
            message="You've used over 75% of your weekly usage limit.",
        ),
        ev("session.error", errorType="session", statusCode=429, message="You've hit your rate limit."),
        ev("session.error", errorType="session", statusCode=401, message="later"),
    ]
    _, res = run(events, {"type": "argus.exit", "ok": False, "error": "exit_status"})
    assert res.warnings and res.failure.category == "rate_limit"
    assert res.failure.message == "You've hit your rate limit."


# --------------------------------------------------------------- end to end with a fake copilot

FAKE_COPILOT = """
import json, os, sys, time, uuid
argv = sys.argv[1:]
for a in argv:
    for bad in ("--allow-all", "--yolo", "--autopilot", "--trust-folder", "--allow-tool", "--enable-memory"):
        assert not a.startswith(bad), a
if argv[:1] == ["login"]:
    assert argv == ["login", "--device-code"], argv
    assert os.environ.get("BROWSER") == "/bin/true"
    settings = json.load(open(os.path.join(os.environ["HOME"], ".copilot", "settings.json")))
    assert settings["storeTokenPlaintext"] is True and settings["theme"] == "kept"
    assert "COPILOT_GITHUB_TOKEN" not in os.environ
    print("To authenticate, visit https://github.com/login/device and enter code 4A1F-9C2B", flush=True)
    print("Waiting for authorization...", flush=True)
    time.sleep(float(os.environ.get("FAKE_WAIT", "0.5")))
    path = os.path.join(os.environ["HOME"], ".copilot", "config.json")
    with open(path, "w") as f:
        json.dump({"loggedInUsers": [{"host": "https://github.com", "login": "octo"}],
                   "copilotTokens": {"https://github.com:octo": "gho_secret"}}, f)
    print("Signed in successfully as octo.")
    sys.exit(0)
assert "--available-tools=" in argv and "--deny-tool=shell" in argv
prompt = sys.stdin.read()
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else str(uuid.uuid4())
usage_path = argv[argv.index("--usage-output-file") + 1]
state = os.path.join(os.environ["HOME"], ".copilot", "session-state", sid)
resumed = os.path.isdir(state)
os.makedirs(state, exist_ok=True)
def emit(kind, **data):
    print(json.dumps({"id": str(uuid.uuid4()), "timestamp": "t", "parentId": None, "type": kind, "data": data}),
          flush=True)
if resumed:
    emit("session.resume", resumeTime="t", eventCount=3, selectedModel="claude-sonnet-5.5")
else:
    emit("session.start", sessionId=sid, version=1, producer="copilot-agent", copilotVersion="1.0.108",
         startTime="t", selectedModel="claude-sonnet-5.5")
if "use a tool" in prompt:
    emit("tool.execution_start", toolCallId="c1", toolName="bash", arguments={"command": "id"})
if "hit the limit" in prompt:
    emit("model.call_failure", source="top_level", statusCode=429, errorMessage="rate limited")
    emit("session.error", errorType="rate_limit", statusCode=429, message="You've hit your rate limit.")
    print(json.dumps({"type": "result", "sessionId": sid, "exitCode": 1, "usage": {}}))
    sys.exit(1)
report = {"argv": argv, "prompt": prompt, "resumed": resumed,
          "env": {k: v for k, v in os.environ.items() if k.startswith(("COPILOT_", "GH_", "GITHUB_"))}}
emit("assistant.message_delta", messageId="m1", deltaContent=json.dumps(report))
emit("assistant.message", messageId="m1", content=json.dumps(report), model="claude-sonnet-5.5", toolRequests=[])
emit("assistant.turn_end", turnId="0")
with open(usage_path, "w") as f:
    n = 2 if resumed else 1
    json.dump({"totalPremiumRequestCost": n, "totalUserRequests": n, "totalApiDurationMs": 5,
               "sessionStartTime": "2026-11-10T00:00:00Z",
               "codeChanges": {"linesAdded": 0, "linesRemoved": 0, "filesModified": []},
               "modelMetrics": {"claude-sonnet-5.5": {"requests": {"count": n, "cost": n},
                   "usage": {"inputTokens": 100 * n, "outputTokens": 10 * n, "cacheReadTokens": 0,
                             "cacheWriteTokens": 0}}},
               "currentModel": "claude-sonnet-5.5", "lastCallInputTokens": 100, "lastCallOutputTokens": 10}, f)
print(json.dumps({"type": "result", "timestamp": "t", "sessionId": sid, "exitCode": 0,
                  "usage": {"premiumRequests": n, "totalApiDurationMs": 5, "sessionDurationMs": 9}}))
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
async def runner(tmp_path: Path):
    bins = tmp_path / "bin"
    bins.mkdir()
    r = Runner(
        RunnerConfig(
            home_root=tmp_path / "users", binaries=Binaries(copilot=_exe(bins / "copilot", FAKE_COPILOT))
        )
    )
    yield r
    await r.logins.close()


async def collect(runner: Runner, **kw):
    events = [e async for e in runner.invoke(InvokeSpec(user_id=USER_A, cli="copilot", **kw))]
    parser = make_parser("copilot")
    for e in events[:-1]:
        parser.feed(e)
    return events, parser.finish(events[-1])


async def test_a_turn_creates_the_session_argus_chose_and_resumes_it(runner):
    sid = str(uuid.uuid4())
    events, res = await collect(
        runner,
        prompt="hi",
        system_prompt="You are a council seat.",
        session_id=sid,
        api_key="github_pat_abc123",
    )
    exit_ = events[-1]
    assert exit_["ok"], exit_
    assert exit_["session_id"] == sid and res.session_id == sid
    report = json.loads(res.text)
    assert report["prompt"].startswith("<system_instructions>\nYou are a council seat.\n")
    assert report["prompt"].endswith("\n\nhi") and not report["resumed"]
    assert report["env"]["COPILOT_GITHUB_TOKEN"] == "github_pat_abc123"
    assert report["env"]["COPILOT_AUTO_UPDATE"] == "false" and "GH_TOKEN" not in report["env"]
    assert res.usage.input_tokens == 100 and res.usage.output_tokens == 10
    assert any(e["type"] == "argus.result_file" for e in events)
    assert runner.has_session(USER_A, "copilot", sid)

    events, again = await collect(runner, prompt="more", session_id=sid, resume=True, system_prompt="ignored")
    assert events[-1]["ok"] and again.session_id == sid
    report = json.loads(again.text)
    assert report["resumed"] and report["prompt"] == "more"
    assert again.usage.input_tokens == 200  # a session total; the adapter takes the difference


async def test_a_rate_limited_turn_falls_back(runner):
    events, res = await collect(runner, prompt="hit the limit", session_id=str(uuid.uuid4()))
    assert not events[-1]["ok"]
    assert res.failure is not None and res.failure.category == "rate_limit"


async def test_a_tool_that_runs_kills_the_turn(runner):
    events, res = await collect(runner, prompt="use a tool", session_id=str(uuid.uuid4()))
    assert events[-1]["error"] == "tools_not_disabled"
    assert not any(e["type"] == "argus.result_file" for e in events)
    assert res.failure is not None and res.failure.category not in FALLBACK_CATEGORIES


async def _wait(runner: Runner, *states: str) -> dict:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        st = runner.logins.state(USER_A, "copilot")
        if st["state"] in states:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(runner.logins.state(USER_A, "copilot"))


async def test_device_code_sign_in_status_and_sign_out(runner):
    home = runner.cfg.home_root / USER_A
    (home / ".copilot").mkdir(parents=True)
    (home / ".copilot" / "settings.json").write_text(json.dumps({"theme": "kept"}))
    status = await runner.status(USER_A, "copilot")
    assert status["logged_in"] is None and status["verified"] is False

    st = await runner.logins.start(USER_A, "copilot")
    assert st["flow"] == "device_code"
    st = await _wait(runner, "waiting")
    assert st["verification_url"] == "https://github.com/login/device"
    assert st["user_code"] == "4A1F-9C2B" and not st["needs_code"]
    st = await _wait(runner, "succeeded")
    assert st["error"] is None and "gho_secret" not in json.dumps(st)

    status = await runner.status(USER_A, "copilot")
    assert status["logged_in"] is True and status["verified"] is False
    assert "octo" not in json.dumps(status) and "gho_secret" not in json.dumps(status)

    out = await runner.logout(USER_A, "copilot")
    assert out["ok"] and not (home / ".copilot" / "config.json").exists()
    assert out["status"]["logged_in"] is None
    assert json.loads((home / ".copilot" / "settings.json").read_text())["theme"] == "kept"
