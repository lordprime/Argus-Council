"""Kiro CLI as a council seat, driven over its Agent Client Protocol mode (``kiro-cli acp``).

Kiro itself is replaced by a fake ACP agent (a Python script) that speaks newline-delimited
JSON-RPC 2.0 the way the ACP spec and Kiro's ACP docs describe: ``initialize``, ``session/new`` or
``session/load`` (with history replay), ``session/prompt`` with streamed ``session/update``
notifications, agent->client requests (permission, fs, terminal), and JSON-RPC errors.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from argus.providers.cli.parsers import make_parser
from argus.providers.cli.vendors.kiro import KiroParser, classify_kiro
from argus.runner.acp import acp_turn
from argus.runner.clis import key_env, login_spec_for, logout_spec_for, plugin
from argus.runner.clis.kiro import CAPABILITIES, PLUGIN
from argus.runner.process import Runner, RunnerConfig
from argus.runner.specs import Binaries, InvokeSpec, TurnContext, build_command

USER = "11111111-1111-4111-8111-111111111111"
SID = "0b6a1f9e-4c1d-4a8e-9f3b-2d7c5e8a9b10"
FORBIDDEN = ("--trust-all-tools", "-a", "--trust-tools", "--cloud")
POLICY = Path(__file__).resolve().parents[1] / "docker" / "runner" / "policies" / "kiro-managed-settings.json"

FAKE_ACP = r"""
import json, os, sys, time

MODE = os.environ.get("FAKE_MODE", "ok")
HOME = os.environ.get("KIRO_HOME") or os.getcwd()
LOG = os.environ.get("FAKE_LOG") or os.path.join(HOME, "fake-log.jsonl")
SID = "0B6A1F9E-4C1D-4A8E-9F3B-2D7C5E8A9B10"


def log(obj):
    with open(LOG, "a") as f:
        f.write(json.dumps(obj) + "\n")


def send(obj):
    obj = {"jsonrpc": "2.0", **obj}
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def recv():
    line = sys.stdin.readline()
    if not line:
        log({"eof": True})
        sys.exit(0)
    return json.loads(line)


def drain():
    while True:
        recv()


def update(sid, upd):
    send({"method": "session/update", "params": {"sessionId": sid, "update": upd}})


def chunk(sid, text, kind="agent_message_chunk"):
    update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})


def ask(rid, method, params):
    send({"id": rid, "method": method, "params": params})
    reply = recv()
    log({"asked": method, "reply": reply})


if sys.argv[1:2] == ["acp"]:
    agent_file = os.path.join(os.getcwd(), ".kiro", "agents", "argus-seat.json")
    agent = json.load(open(agent_file))
    prompt = agent.get("prompt", "")
    system = open(prompt[len("file://"):]).read() if prompt.startswith("file://") else None
    settings = json.load(open(os.path.join(os.environ["KIRO_HOME"], "settings", "cli.json")))
    log({"argv": sys.argv[1:], "agent": agent, "system": system, "settings": settings,
         "has_key": "KIRO_API_KEY" in os.environ})

sys.stdout.write("Kiro ACP server starting (not JSON)\n")
sys.stdout.flush()
req = recv()
log({"init": req})
if MODE == "eof":
    sys.exit(3)
send({"id": req["id"], "result": {"protocolVersion": 1,
      "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True}},
      "agentInfo": {"name": "kiro-cli", "version": "3.1.0"}, "authMethods": []}})

req = recv()
log({"session": req})
if MODE == "auth":
    send({"id": req["id"], "error": {"code": -32000, "message": "Authentication required"}})
    drain()
if req["method"] == "session/new":
    sid = SID
    send({"id": req["id"], "result": {"sessionId": sid, "models": {"currentModelId": "claude-sonnet-4.6"}}})
else:
    sid = req["params"]["sessionId"]
    update(sid, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "old question"}})
    chunk(sid, "OLD ANSWER")
    send({"id": req["id"], "result": None})
send({"method": "_kiro.dev/commands/available", "params": {"sessionId": sid, "commands": []}})

req = recv()
log({"prompt": req})
text = req["params"]["prompt"][0]["text"]
if MODE == "ratelimit":
    send({"method": "_kiro.dev/error/rate_limit", "params": {"message": "Throttled, retrying"}})
    send({"id": req["id"], "error": {"code": -32603,
          "message": "429 Too Many Requests: temporary request limits while handling: " + text}})
    drain()
if MODE == "quota":
    send({"id": req["id"], "error": {"code": -32603,
          "message": "You have reached the monthly usage limit for your plan"}})
    drain()
if MODE == "hang":
    time.sleep(30)

ask(100, "session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "t1", "title": "Read file"},
    "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
ask(101, "session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "t2", "title": "Shell"},
    "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]})
ask(102, "fs/read_text_file", {"sessionId": sid, "path": "/etc/passwd"})
ask(103, "terminal/create", {"sessionId": sid, "command": "id"})
sys.stdout.write("a log line that is not JSON\n")
update("another-session", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "LEAK"}})
chunk(sid, "Let me think.", "agent_thought_chunk")
update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t3", "title": "Thinking", "kind": "think",
             "status": "completed"})
chunk(sid, "Hel")
chunk(sid, "lo")
send({"id": req["id"], "result": {"stopReason": "end_turn",
      "usage": {"inputTokens": 12, "outputTokens": 3, "cachedReadTokens": 4}}})
drain()
"""

FAKE_LOGIN = r"""
import json, os, sys, time
argv = sys.argv[1:]
if argv == ["logout"]:
    open(os.path.join(os.environ["HOME"], "logged-out"), "w").write("yes")
    print("You are now logged out"); print("Run kiro-cli login to log back in to Kiro CLI"); sys.exit(0)
assert os.environ.get("BROWSER") == "/bin/true"
methods = {
    ("login", "--license", "free", "--use-device-flow"): "https://view.awsapps.com/start/#/device?user_code=QWER-TYUI",
    ("login", "--license", "free", "--social", "google", "--use-device-flow"): "https://app.kiro.dev/device?code=QWER-TYUI",
    ("login", "--license", "free", "--social", "github", "--use-device-flow"): "https://evil.example/device",
}
url = methods[tuple(argv)]
print("Confirm the following code in the browser")
print("Code: \x1b[1mQWER-TYUI\x1b[0m")
print("")
print("Open this URL: " + url, flush=True)
time.sleep(0.6)
print("Device authorized")
print("Logged in successfully")
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _turn(tmp_path: Path, system: bool = True) -> TurnContext:
    home = tmp_path / "home"
    cwd = home / "work" / "run1"
    turn_dir = home / ".argus" / "turns" / "t1"
    for d in (cwd, turn_dir):
        d.mkdir(parents=True, exist_ok=True)
    prompt_file = None
    if system:
        prompt_file = home / ".argus" / "prompts" / "p.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text("You are seat one.")
    return TurnContext(
        home=str(home),
        cwd=str(cwd),
        turn_dir=str(turn_dir),
        system_prompt_file=str(prompt_file) if prompt_file else None,
    )


def _spec(**kw: Any) -> InvokeSpec:
    return InvokeSpec(user_id=USER, cli="kiro", prompt="What is 2+2?", **kw)


# ----------------------------------------------------------------------------------------- argv


def test_argv_new_session_tools_off(tmp_path: Path) -> None:
    turn = _turn(tmp_path)
    cmd = build_command(_spec(), binaries=Binaries(), system_prompt_file=turn.system_prompt_file, turn=turn)
    assert cmd.argv == ["kiro-cli", "acp", "--agent-engine", "v2", "--agent", "argus-seat"]
    assert cmd.protocol == "acp"
    assert cmd.stdin == "What is 2+2?"
    assert cmd.env == {"KIRO_HOME": os.path.join(turn.home, ".kiro")}
    assert not any(flag in cmd.argv for flag in FORBIDDEN)
    agent_path = os.path.join(turn.cwd, ".kiro", "agents", "argus-seat.json")
    agent = json.loads(cmd.files[agent_path])
    assert agent["name"] == "argus-seat"
    assert agent["tools"] == [] and agent["allowedTools"] == []
    assert agent["includeMcpJson"] is False and agent["mcpServers"] == {}
    assert agent["prompt"] == "file://" + turn.system_prompt_file
    settings = json.loads(cmd.files[os.path.join(turn.home, ".kiro", "settings", "cli.json")])
    assert settings["chat.enableKnowledge"] is False
    assert settings["chat.enableCodeIntelligence"] is False
    assert settings["chat.disableInheritingDefaultResources"] is True
    for path in cmd.files:
        assert Path(path).resolve().is_relative_to(Path(turn.home).resolve())


def test_argv_model_key_resume_and_no_system_prompt(tmp_path: Path) -> None:
    turn = _turn(tmp_path, system=False)
    spec = _spec(model="claude-sonnet-4.6", api_key="ksk_test_123456789", session_id=SID, resume=True)
    cmd = PLUGIN.build(spec, Binaries(kiro="/opt/clis/bin/kiro-cli"), turn)
    assert cmd.argv == [
        "/opt/clis/bin/kiro-cli", "acp", "--agent-engine", "v2", "--agent", "argus-seat", "--model", "claude-sonnet-4.6"
    ]  # fmt: skip
    assert cmd.env["KIRO_API_KEY"] == "ksk_test_123456789"
    assert not any(flag in cmd.argv for flag in FORBIDDEN)
    agent = json.loads(next(v for k, v in cmd.files.items() if k.endswith("argus-seat.json")))
    assert "prompt" not in agent
    assert key_env("kiro", None, "ksk_x", None) == {"KIRO_API_KEY": "ksk_x"}


def test_kiro_assigns_session_ids_and_models_are_checked() -> None:
    with pytest.raises(ValueError):
        _spec(session_id=SID)  # Kiro assigns ids: only pass one to resume
    with pytest.raises(ValueError):
        _spec(model="--trust-all-tools")
    assert _spec(persist=False).persist is False


def test_policy_denies_every_capability() -> None:
    policy = json.loads(POLICY.read_text())
    assert set(policy) == {"rules"}  # unknown fields would make Kiro reject the whole file
    assert all(set(r) == {"capability", "effect"} and r["effect"] == "deny" for r in policy["rules"])
    assert [r["capability"] for r in policy["rules"]] == list(CAPABILITIES)


# ----------------------------------------------------------------------------------- ACP driver


async def _drive(
    tmp_path: Path, mode: str, *, resume: bool = False, timeout_s: float = 20
) -> tuple[list[dict], list[dict]]:
    script = tmp_path / "fake_acp.py"
    script.write_text(FAKE_ACP)
    log = tmp_path / f"log-{mode}-{resume}.jsonl"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "FAKE_MODE": mode, "FAKE_LOG": str(log)},
    )
    events = []
    try:
        async for event in acp_turn(
            proc,
            prompt="What is 2+2? Please answer briefly.",
            session_id=SID if resume else None,
            resume=resume,
            cwd=str(tmp_path),
            timeout_s=timeout_s,
        ):
            events.append(event)
        if mode != "hang":  # a hung agent is killed below, as the runner does
            await asyncio.wait_for(proc.wait(), 10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    lines = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return events, lines


def _parse(events: list[dict], exit_ok: bool = True) -> Any:
    parser = make_parser("kiro")
    assert isinstance(parser, KiroParser)
    streamed = [s for e in events for s in parser.feed(e)]
    exit_record = {
        "type": "argus.exit",
        "ok": exit_ok,
        "error": None if exit_ok else "exit_status",
        "detail": "",
    }
    return parser.finish(exit_record), streamed


async def test_new_session_streams_and_refuses_everything(tmp_path: Path) -> None:
    events, log = await _drive(tmp_path, "ok")
    init = log[0]["init"]
    assert init["method"] == "initialize"
    assert init["params"]["protocolVersion"] == 1
    assert init["params"]["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }
    session = log[1]["session"]
    assert session["method"] == "session/new"
    assert session["params"] == {"cwd": str(tmp_path), "mcpServers": []}
    prompt = log[2]["prompt"]["params"]
    assert prompt["sessionId"] == SID.upper()
    assert prompt["prompt"] == [{"type": "text", "text": "What is 2+2? Please answer briefly."}]

    replies = {
        entry["asked"]: entry["reply"] for entry in log if "asked" in entry and entry["reply"]["id"] != 101
    }
    assert replies["session/request_permission"]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "no"}
    }
    no_reject = next(e["reply"] for e in log if "asked" in e and e["reply"]["id"] == 101)
    assert no_reject["result"] == {"outcome": {"outcome": "cancelled"}}
    for method in ("fs/read_text_file", "terminal/create"):
        assert "result" not in replies[method]
        assert replies[method]["error"]["code"] == -32601
    assert log[-1] == {"eof": True}  # the driver closes stdin after the result

    kinds = [e["type"] for e in events]
    assert kinds[0] == "acp.session" and kinds[-1] == "acp.result"
    assert events[0]["sessionId"] == SID.upper()
    assert all("LEAK" not in json.dumps(e) for e in events)  # another session's update is dropped
    assert {"type": "acp.notification", "method": "_kiro.dev/commands/available"}.items() <= events[1].items()
    assert PLUGIN.session_from_event(events[0]) == SID
    assert all(PLUGIN.tools_reported(e) is None for e in events)  # a "think" tool call is not a tool

    result, streamed = _parse(events)
    assert result.failure is None
    assert result.text == "Hello"
    assert [s.text for s in streamed if s.type == "text"] == ["Hel", "lo"]
    assert result.thinking == "Let me think."
    assert result.session_id == SID.upper()
    assert result.model == "claude-sonnet-4.6"
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cache_read_tokens) == (
        12,
        3,
        4,
    )


async def test_resume_loads_session_and_drops_replayed_history(tmp_path: Path) -> None:
    events, log = await _drive(tmp_path, "ok", resume=True)
    session = log[1]["session"]
    assert session["method"] == "session/load"
    assert session["params"] == {"sessionId": SID, "cwd": str(tmp_path), "mcpServers": []}
    assert events[0] == {"type": "acp.session", "sessionId": SID}
    assert "OLD ANSWER" not in json.dumps(events) and "old question" not in json.dumps(events)
    result, _ = _parse(events)
    assert result.text == "Hello" and result.failure is None


async def test_rate_limit_error_is_classified_and_prompt_not_echoed(tmp_path: Path) -> None:
    events, _ = await _drive(tmp_path, "ratelimit")
    last = events[-1]
    assert last["type"] == "acp.error" and last["reason"] == "rpc" and last["stage"] == "session/prompt"
    assert last["error"]["code"] == -32603
    assert "What is 2+2" not in json.dumps(events)
    assert "[prompt]" in last["error"]["message"]
    result, _ = _parse(events, exit_ok=True)
    assert result.failure is not None and result.failure.category == "rate_limit"


async def test_quota_and_auth_errors(tmp_path: Path) -> None:
    events, _ = await _drive(tmp_path, "quota")
    result, _ = _parse(events)
    assert result.failure is not None and result.failure.category == "quota"

    events, _ = await _drive(tmp_path, "auth")
    assert events[-1]["stage"] == "session/new" and events[-1]["error"]["code"] == -32000
    assert not any(e["type"] == "acp.session" for e in events)
    result, _ = _parse(events)
    assert result.failure is not None and result.failure.category == "auth"


async def test_agent_exits_early_and_timeout(tmp_path: Path) -> None:
    events, _ = await _drive(tmp_path, "eof")
    assert events[-1]["type"] == "acp.error" and events[-1]["reason"] == "eof"
    result, _ = _parse(events, exit_ok=False)
    assert result.failure is not None and result.failure.category == "unavailable"

    start = time.monotonic()
    events, _ = await _drive(tmp_path, "hang", timeout_s=1)
    assert time.monotonic() - start < 10
    assert events[-1]["reason"] == "timeout"


# --------------------------------------------------------------------------------------- parser


def test_parser_stop_reasons_and_error_texts() -> None:
    for stop in ("refusal", "max_tokens"):
        p = KiroParser()
        p.feed(
            {
                "type": "acp.update",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x"}},
            }
        )
        p.feed({"type": "acp.result", "stopReason": stop})
        res = p.finish({"ok": True})
        assert res.failure is not None and res.failure.category == "error"
    assert classify_kiro(-32000, "Authentication required") == "auth"
    assert (
        classify_kiro(-32603, "The bearer token included in the request is invalid: not logged in") == "auth"
    )
    assert classify_kiro(-32603, "ThrottlingException: Rate exceeded") == "rate_limit"
    assert classify_kiro(-32603, "Too many requests, please slow down") == "rate_limit"
    assert classify_kiro(-32603, "You have used all of your credits for this month") == "quota"
    assert classify_kiro(-32602, "Invalid params") == "invalid"


def test_parser_result_then_untidy_exit_is_success_and_no_result_is_failure() -> None:
    p = KiroParser()
    p.feed(
        {
            "type": "acp.update",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}},
        }
    )
    p.feed({"type": "acp.result", "stopReason": "end_turn"})
    res = p.finish({"ok": False, "error": "exit_status", "exit_code": -9, "detail": ""})
    assert res.failure is None and res.text == "ok"

    p = KiroParser()
    p.feed({"type": "acp.result", "stopReason": "end_turn"})
    res = p.finish({"ok": False, "error": "tools_not_disabled", "detail": "kiro reported 1 tools of its own"})
    assert res.failure is not None and res.failure.category == "error"

    p = KiroParser()
    res = p.finish({"ok": True})
    assert res.failure is not None and res.failure.category == "unavailable"

    p = KiroParser()
    p.feed(
        {
            "type": "acp.update",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "a",
                "title": "Run shell",
                "kind": "execute",
            },
        }
    )
    p.feed({"type": "acp.result", "stopReason": "end_turn"})
    res = p.finish({"ok": True})
    assert res.warnings and "Run shell" in res.warnings[0]


def test_tools_reported_and_session_ids() -> None:
    call = {
        "type": "acp.update",
        "update": {"sessionUpdate": "tool_call", "toolCallId": "a", "title": "Run shell", "kind": "execute"},
    }
    assert PLUGIN.tools_reported(call) == ["Run shell"]
    think = {
        "type": "acp.update",
        "update": {"sessionUpdate": "tool_call", "toolCallId": "b", "kind": "think"},
    }
    assert PLUGIN.tools_reported(think) is None
    assert PLUGIN.tools_reported(
        {"type": "acp.notification", "method": "_kiro.dev/mcp/server_initialized"}
    ) == ["mcp"]
    assert PLUGIN.tools_reported({"type": "acp.notification", "method": "_kiro/customAgent/not_found"})
    assert PLUGIN.tools_reported({"type": "acp.result", "stopReason": "end_turn"}) is None
    assert PLUGIN.session_from_event({"type": "acp.session", "sessionId": "sess_abc123"}) is None
    assert PLUGIN.session_from_event({"type": "acp.update", "sessionId": SID}) is None


# ------------------------------------------------------------------------ runner, end to end


async def test_runner_drives_kiro_turn_and_records_session(tmp_path: Path) -> None:
    bins = tmp_path / "bin"
    bins.mkdir()
    runner = Runner(
        RunnerConfig(home_root=tmp_path / "users", binaries=Binaries(kiro=_exe(bins / "kiro-cli", FAKE_ACP)))
    )
    spec = InvokeSpec(
        user_id=USER,
        cli="kiro",
        prompt="hi",
        system_prompt="Be terse.",
        api_key="ksk_abcdefghijklmnop",
        run_key="r1",
    )
    events = [e async for e in runner.invoke(spec)]
    exit_record = events[-1]
    assert exit_record["type"] == "argus.exit"
    assert exit_record["ok"] is True, exit_record
    assert exit_record["session_id"] == SID
    assert runner.has_session(USER, "kiro", SID)
    home = tmp_path / "users" / USER
    log = [json.loads(x) for x in (home / ".kiro" / "fake-log.jsonl").read_text().splitlines()]
    start = log[0]
    assert start["argv"] == ["acp", "--agent-engine", "v2", "--agent", "argus-seat"]
    assert start["agent"]["tools"] == [] and start["system"] == "Be terse."
    assert start["settings"]["chat.enableKnowledge"] is False
    assert start["has_key"] is True
    parser = make_parser("kiro")
    for e in events[:-1]:
        parser.feed(e)
    result = parser.finish(exit_record)
    assert result.text == "Hello" and result.failure is None

    resumed = InvokeSpec(user_id=USER, cli="kiro", prompt="again", session_id=SID, resume=True)
    events = [e async for e in runner.invoke(resumed)]
    assert events[-1]["ok"] is True
    assert events[0] == {"type": "acp.session", "sessionId": SID}


# -------------------------------------------------------------------------- sign-in and status


async def _wait(runner: Runner, *states: str) -> dict:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        st = runner.logins.state(USER, "kiro")
        if st["state"] in states:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(runner.logins.state(USER, "kiro"))


async def test_device_flow_sign_in(tmp_path: Path) -> None:
    bins = tmp_path / "bin"
    bins.mkdir()
    runner = Runner(
        RunnerConfig(
            home_root=tmp_path / "users", binaries=Binaries(kiro=_exe(bins / "kiro-cli", FAKE_LOGIN))
        )
    )
    spec = login_spec_for("kiro")
    assert spec is not None and spec.flow == "device_code"
    assert spec.methods == ("builder_id", "google", "github")
    try:
        for method, host in (("builder_id", "view.awsapps.com"), ("google", "app.kiro.dev")):
            await runner.logins.start(USER, "kiro", method)
            st = await _wait(runner, "waiting", "succeeded")
            if st["state"] == "waiting":
                assert st["user_code"] == "QWER-TYUI"
                assert st["verification_url"].startswith(f"https://{host}/")
            st = await _wait(runner, "succeeded")
            assert st["error"] is None

        await runner.logins.start(USER, "kiro", "github")  # prints a link off the allow-list
        st = await _wait(runner, "failed")
        assert st["error"] == "unexpected_output" and st["verification_url"] is None
    finally:
        await runner.logins.close()


def test_status_and_logout() -> None:
    assert logout_spec_for("kiro") == (("logout",), ())
    assert plugin("kiro").status_args == ("whoami", "--format", "json")
    parse = PLUGIN.parse_status
    assert parse is not None
    st = parse(0, json.dumps({"accountType": "BuilderId", "email": "someone@example.com", "profile": "p"}))
    assert st == {"logged_in": True, "method": "builder_id", "verified": True, "detail": ""}
    assert "someone" not in json.dumps(st)
    assert parse(0, '{"authMethod": "Social (GitHub)"}')["method"] == "github"
    assert parse(1, "Not logged in. Run kiro-cli login")["logged_in"] is False
    assert parse(None, "timed out")["logged_in"] is None
    assert parse(2, "panic: something odd")["logged_in"] is None
