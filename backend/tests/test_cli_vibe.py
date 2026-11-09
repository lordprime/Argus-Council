"""Mistral Vibe (``vibe`` 3.1.0) as a council seat: argv, environment, the export.json result file,
session read-back, the executed-tool check and fallback categories.

Vibe itself is replaced by a small fake executable that prints completed history entries the way
``vibe -p --output streaming`` does and writes ``export.json`` into ``--output-dir``.
"""

from __future__ import annotations

import stat
import sys
import uuid
from pathlib import Path

import pytest

from argus.providers.cli.errors import FALLBACK_CATEGORIES
from argus.providers.cli.parsers import make_parser
from argus.providers.cli.vendors.vibe import Parser
from argus.runner.clis import key_env, login_spec_for, logout_spec_for, plugin
from argus.runner.process import Runner, RunnerConfig
from argus.runner.specs import Binaries, InvokeSpec, TurnContext, build_command

USER = "11111111-1111-4111-8111-111111111111"
SID = "0a1b2c3d-4e5f-6a7b-8c9d-0e1f2a3b4c5d"
FORBIDDEN = {
    "--auto-approve",
    "--yolo",
    "--smart-approve",
    "--trust",
    "--enabled-tools",
    "--add-dir",
    "--worktree",
}
TURN = TurnContext(
    home="/home/runner/users/u",
    cwd="/home/runner/users/u/work/r",
    turn_dir="/home/runner/users/u/.argus/turns/t1",
    system_prompt_file="/home/runner/users/u/.argus/prompts/p.md",
)


def _spec(**kw) -> InvokeSpec:
    base = {"user_id": USER, "cli": "vibe", "prompt": "the prompt"}
    return InvokeSpec(**(base | kw))


def _cmd(turn: TurnContext = TURN, **kw):
    return build_command(
        _spec(**kw), binaries=Binaries(), system_prompt_file=turn.system_prompt_file, turn=turn
    )


# ----------------------------------------------------------------------------------------- argv


def test_new_session_argv_turns_every_tool_off_and_reads_the_prompt_from_stdin():
    cmd = _cmd(system_prompt="be terse")
    assert cmd.argv == [
        "vibe",
        "-p",
        "--output",
        "streaming",
        "--agent",
        "ask",
        "--disabled-tools",
        "*",
        "--system-prompt-file",
        TURN.system_prompt_file,
        "--output-dir",
        f"{TURN.turn_dir}/vibe-out",
    ]
    assert cmd.stdin == "the prompt" and "the prompt" not in cmd.argv
    assert cmd.result_file == f"{TURN.turn_dir}/vibe-out/export.json"
    assert cmd.protocol == "stdio" and cmd.files == {}
    assert cmd.env == {
        "VIBE_HOME": f"{TURN.home}/.vibe",
        "VIBE_ENABLE_SUBAGENTS": "false",
        "VIBE_ENABLE_BACKGROUND_PROCESSES": "false",
        "VIBE_ENABLE_TELEMETRY": "false",
    }


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"model": "mistral-medium-3.5"},
        {"session_id": SID, "resume": True},
        {"persist": False},
        {"api_key": "mistral-key-123"},
    ],
)
def test_tools_off_flags_always_present_and_forbidden_flags_never(kw):
    cmd = _cmd(**kw)
    i = cmd.argv.index("--disabled-tools")
    assert cmd.argv[i + 1] == "*"
    assert cmd.argv[cmd.argv.index("--agent") + 1] == "ask"
    assert not FORBIDDEN & set(cmd.argv)
    assert cmd.env["VIBE_ENABLE_SUBAGENTS"] == "false"
    assert cmd.env["VIBE_ENABLE_BACKGROUND_PROCESSES"] == "false"
    assert cmd.env["VIBE_ENABLE_TELEMETRY"] == "false"


def test_model_only_on_a_new_session_and_resume_by_id():
    assert _cmd(model="mistral-medium-3.5").argv[-2:] == ["--model", "mistral-medium-3.5"]
    resumed = _cmd(session_id=SID, resume=True, model="mistral-medium-3.5")
    assert resumed.argv[-2:] == ["--resume", SID]
    assert "--model" not in resumed.argv  # Vibe rejects --model on a resumed session


def test_new_session_never_names_an_id_because_vibe_assigns_it():
    assert "--resume" not in _cmd().argv
    with pytest.raises(ValueError):
        _spec(session_id=SID)  # vibe assigns its own ids: only valid with resume


def test_one_shot_turns_session_logging_off():
    assert _cmd(persist=False).env["VIBE_SESSION_LOGGING__ENABLED"] == "false"
    assert "VIBE_SESSION_LOGGING__ENABLED" not in _cmd().env


def test_no_system_prompt_file_without_a_system_prompt():
    turn = TurnContext(home=TURN.home, cwd=TURN.cwd, turn_dir=TURN.turn_dir, system_prompt_file=None)
    assert "--system-prompt-file" not in _cmd(turn).argv


def test_api_key_goes_to_mistral_api_key_only():
    cmd = _cmd(api_key="mistral-key-123")
    assert cmd.env["MISTRAL_API_KEY"] == "mistral-key-123"
    assert "mistral-key-123" not in cmd.argv
    assert "MISTRAL_API_KEY" not in _cmd().env
    assert key_env("vibe", None, "k", None) == {"MISTRAL_API_KEY": "k"}
    assert key_env("vibe", "anything", "k", "m") == {"MISTRAL_API_KEY": "k"}


def test_plugin_metadata_keys_only():
    p = plugin("vibe")
    assert p.name == "vibe" and p.label == "Mistral Vibe"
    assert p.key_env == "MISTRAL_API_KEY" and not p.key_providers
    assert p.login is None and login_spec_for("vibe") is None
    assert logout_spec_for("vibe") == (None, ())
    assert p.status_args is None and p.parse_status is None and p.status_files == ()
    assert p.supports_effort is False
    assert "default" in p.suggested_models


# --------------------------------------------------------------------------------- output hooks


def test_session_id_is_read_from_the_result_file_event():
    hook = plugin("vibe").session_from_event
    assert hook({"type": "argus.result_file", "data": {"session_id": SID}}) == SID
    assert hook({"type": "argus.result_file", "data": {"session_id": None}}) is None
    assert hook({"type": "argus.result_file", "data": ["not", "a", "dict"]}) is None
    assert hook({"type": "message", "session_id": SID}) is None


def _effect(tool: str, **state) -> dict:
    return {
        "id": "e1",
        "type": "effect",
        "generationStatus": "completed",
        "title": tool,
        "detail": {"kind": "tool", "toolName": tool},
        "state": state,
    }


def test_tools_reported_flags_any_tool_that_executed():
    hook = plugin("vibe").tools_reported
    assert hook(_effect("bash", status="completed", decision="execute")) == ["bash"]
    assert hook(_effect("read_file", status="failed", decision="execute")) == ["read_file"]
    assert hook(_effect("web_fetch", status="completed")) == ["web_fetch"]


def test_tools_reported_ignores_denied_calls_and_the_compute_sandbox():
    hook = plugin("vibe").tools_reported
    assert hook(_effect("bash", status="skipped", reason="denied", decision="skip")) is None
    assert hook(_effect("bash", status="failed", decision="skip")) is None
    assert hook(_effect("bash", status="cancelled", reason="no approver")) is None
    assert hook(_effect("run_typescript", status="completed", decision="execute")) is None
    assert hook({"type": "message", "role": "assistant", "content": []}) is None
    assert hook({"type": "argus.result_file", "data": {}}) is None


# ---------------------------------------------------------------------------------------- parser


def _msg(entry_id: str, text: str, role: str = "assistant", **kw) -> dict:
    return {
        "id": entry_id,
        "turnId": "t1",
        "type": "message",
        "generationStatus": "completed",
        "role": role,
        "content": [{"type": "text", "text": text}],
        **kw,
    }


def _export(**kw) -> dict:
    data = {
        "outcome": "finished",
        "stop_reason": None,
        "error": None,
        "session_id": SID,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 20, "total_tokens": 150},
        "cost_usd": 0.0042,
        "steps": 1,
        "config": {"active_model": "mistral-medium-3.5"},
        "schema_version": 1,
        "vibe_version": "3.1.0",
        "exit_code": 0,
    }
    return {"type": "argus.result_file", "data": data | kw}


OK_EXIT = {"type": "argus.exit", "ok": True, "exit_code": 0, "error": None, "detail": ""}


def test_make_parser_loads_the_vibe_parser():
    assert isinstance(make_parser("vibe"), Parser)


def test_text_is_emitted_once_per_completed_assistant_message():
    p = Parser()
    out = []
    for ev in [
        _msg("u1", "the prompt", role="user", source="turn_start"),
        {"id": "r1", "type": "reasoning", "generationStatus": "completed", "text": "thinking..."},
        _msg("a1", "Hello"),
        _msg("a1", "Hello"),  # the same entry again
        _msg("a2", "draft", outcome={"type": "discarded", "cause": "retry"}),
        _msg("a3", "partial", generationStatus="in_progress"),
        _msg("a4", "World", outcome={"type": "committed"}),
        {"id": "n1", "type": "notice", "level": "warning", "message": "skill not loaded"},
        _effect("run_typescript", status="completed", decision="execute"),
    ]:
        out += p.feed(ev)
    assert [(e.type, e.text) for e in out] == [
        ("thinking", "thinking..."),
        ("text", "Hello"),
        ("text", "\n\nWorld"),
    ]
    p.feed(_export())
    r = p.finish(OK_EXIT)
    assert r.text == "Hello\n\nWorld"
    assert r.thinking == "thinking..."
    assert r.failure is None
    assert "skill not loaded" in r.warnings


def test_multiple_text_blocks_in_one_message_are_joined():
    p = Parser()
    ev = _msg("a1", "one")
    ev["content"] = [
        {"type": "text", "text": "one"},
        {"type": "image", "attachment": {}},
        {"type": "text", "text": "two"},
    ]
    assert [e.text for e in p.feed(ev)] == ["one\n\ntwo"]


def test_usage_session_cost_and_model_come_from_the_export():
    p = Parser()
    p.feed(_msg("a1", "hi"))
    p.feed(_export())
    r = p.finish(OK_EXIT | {"session_id": SID})
    assert r.session_id == SID
    assert r.usage.input_tokens == 100 and r.usage.cache_read_tokens == 20 and r.usage.output_tokens == 30
    assert r.reported_cost_usd == pytest.approx(0.0042)
    assert r.model == "mistral-medium-3.5"


def _failed(data: dict, exit_code: int = 2):
    p = Parser()
    p.feed(_export(**data))
    return p.finish(
        {"type": "argus.exit", "ok": False, "exit_code": exit_code, "error": "exit_status", "detail": ""}
    )


@pytest.mark.parametrize(
    ("data", "category"),
    [
        (
            {
                "outcome": "infrastructure_failure",
                "error": {"message": "Rate limits exceeded.", "code": "rate_limit"},
            },
            "rate_limit",
        ),
        ({"outcome": "config_error", "error": {"message": "bad key", "code": "invalid_api_key"}}, "auth"),
        ({"outcome": "config_error", "error": {"message": "Unauthorized", "code": "unauthorized"}}, "auth"),
        (
            {
                "outcome": "config_error",
                "error": {
                    "message": "Missing MISTRAL_API_KEY environment variable for mistral provider",
                    "code": None,
                },
            },
            "auth",
        ),
        (
            {
                "outcome": "infrastructure_failure",
                "error": {"message": "upstream 502", "code": "backend_error"},
            },
            "unavailable",
        ),
        (
            {
                "outcome": "infrastructure_failure",
                "error": {"message": "stream cut", "code": "incomplete_stream"},
            },
            "unavailable",
        ),
        (
            {
                "outcome": "infrastructure_failure",
                "error": {"message": "Monthly usage limit reached for your plan", "code": "rate_limit"},
            },
            "quota",
        ),
        ({"outcome": "price_limit", "error": None}, "quota"),
        ({"outcome": "token_limit", "error": None}, "quota"),
        ({"outcome": "turn_limit", "error": None}, "quota"),
        ({"outcome": "config_error", "error": {"message": "x", "code": "invalid_model"}}, "invalid"),
        (
            {"outcome": "usage_error", "error": {"message": "Unknown model alias 'foo'", "code": None}},
            "invalid",
        ),
        ({"outcome": "length", "error": {"message": "too long", "code": "context_too_long"}}, "invalid"),
        ({"outcome": "terminated", "error": None}, "unavailable"),
    ],
)
def test_export_errors_map_to_fallback_categories(data, category):
    r = _failed(data)
    assert r.failure is not None and r.failure.category == category


def test_error_message_is_kept_and_first_failure_wins():
    r = _failed(
        {
            "outcome": "infrastructure_failure",
            "error": {"message": "Rate limits exceeded.", "code": "rate_limit"},
        }
    )
    assert r.failure.message == "Rate limits exceeded."


def test_no_export_falls_back_to_the_runner_exit():
    p = Parser()
    r = p.finish({"type": "argus.exit", "ok": False, "error": "timeout", "detail": "timed out"})
    assert r.failure.category == "unavailable"


# ------------------------------------------------------------------- end to end with a fake vibe

FAKE_VIBE = """
import json, os, sys, uuid
argv = sys.argv[1:]
stdin = sys.stdin.read()
for bad in ("--auto-approve", "--yolo", "--smart-approve", "--trust"):
    assert bad not in argv, bad
assert argv[argv.index("--disabled-tools") + 1] == "*"
out = argv[argv.index("--output-dir") + 1]
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
def emit(entry):
    print(json.dumps(entry), flush=True)
emit({"id": "u1", "turnId": "t", "type": "message", "generationStatus": "completed", "role": "user",
      "content": [{"type": "text", "text": stdin}]})
if stdin == "use a tool":
    emit({"id": "e1", "type": "effect", "generationStatus": "completed", "title": "bash",
          "detail": {"kind": "shell", "toolName": "bash"},
          "state": {"status": "completed", "decision": "execute"}})
emit({"id": "a1", "turnId": "t", "type": "message", "generationStatus": "completed", "role": "assistant",
      "content": [{"type": "text", "text": "answer to " + stdin}]})
error = None
outcome = "finished"
if stdin == "hit the limit":
    outcome, error = "infrastructure_failure", {"message": "Rate limits exceeded.", "code": "rate_limit"}
os.makedirs(out, exist_ok=True)
export = {"outcome": outcome, "error": error, "session_id": sid,
          "usage": {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0, "total_tokens": 15},
          "cost_usd": 0.001, "schema_version": 1, "vibe_version": "3.1.0",
          "argus_test": {"argv": argv, "env": {k: v for k, v in os.environ.items() if k.startswith(("VIBE_", "MISTRAL_"))}}}
with open(os.path.join(out, "export.json"), "w") as f:
    json.dump(export, f)
sys.exit(2 if error else 0)
"""


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    exe = tmp_path / "bin" / "vibe"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\n{FAKE_VIBE}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return Runner(RunnerConfig(home_root=tmp_path / "users", binaries=Binaries(vibe=str(exe))))


async def _run(runner: Runner, **kw):
    events = [e async for e in runner.invoke(_spec(**kw))]
    parser = Parser()
    for ev in events[:-1]:
        parser.feed(ev)
    return events, parser.finish(events[-1])


async def test_a_turn_reads_back_the_session_and_resumes_it(runner):
    events, result = await _run(runner, system_prompt="be terse", api_key="mistral-key-123")
    exit_ = events[-1]
    assert exit_["ok"], exit_
    assert result.text == "answer to the prompt"
    sid = result.session_id
    assert sid and uuid.UUID(sid) and exit_["session_id"] == sid
    assert runner.has_session(USER, "vibe", sid)
    export = next(e for e in events if e["type"] == "argus.result_file")["data"]["argus_test"]
    home = runner.cfg.home_root / USER
    assert export["env"]["VIBE_HOME"] == str(home / ".vibe")
    assert export["env"]["MISTRAL_API_KEY"] == "mistral-key-123"
    assert "--system-prompt-file" in export["argv"]

    events, again = await _run(runner, session_id=sid, resume=True, prompt="more")
    assert events[-1]["ok"] and again.session_id == sid and again.text == "answer to more"


async def test_a_rate_limited_turn_falls_back(runner):
    events, result = await _run(runner, prompt="hit the limit")
    assert not events[-1]["ok"]
    assert result.failure is not None and result.failure.category == "rate_limit"


async def test_a_tool_that_runs_kills_the_turn(runner):
    events, result = await _run(runner, prompt="use a tool")
    assert events[-1]["error"] == "tools_not_disabled"
    assert not any(e["type"] == "argus.result_file" for e in events)
    assert result.failure is not None and result.failure.category not in FALLBACK_CATEGORIES


async def test_status_reports_an_api_key_cli(runner):
    status = await runner.status(USER, "vibe")
    assert status["logged_in"] is None and status["verified"] is False
