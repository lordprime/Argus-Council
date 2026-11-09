"""Qwen Code (``qwen`` 0.27.1) as a council seat: argv, key env, the operator settings file, the
stream-json parser and a full turn through the runner against a fake ``qwen`` executable that prints
what the pinned version prints.
"""

from __future__ import annotations

import json
import stat
import sys
import uuid
from pathlib import Path

import pytest

from argus.providers.cli.parsers import make_parser
from argus.runner.clis import key_env, login_spec_for, logout_spec_for, plugin
from argus.runner.login import LoginError
from argus.runner.process import Runner, RunnerConfig
from argus.runner.specs import Binaries, InvokeSpec, TurnContext, build_command

USER_A = "11111111-1111-4111-8111-111111111111"
POLICY = Path(__file__).resolve().parents[1] / "docker" / "runner" / "policies" / "qwen-settings.json"

# Canonical built-in tool names of @qwen-code/qwen-code 0.27.1 (its ToolNames table). There is no
# "*" for built-in tools, so the operator file must list every one; re-check on each version bump.
QWEN_TOOLS = (
    "advisor agent artifact ask_user_question autofix_pr create_sub_session cron_create cron_delete "
    "cron_list display_image edit enter_plan_mode enter_worktree exec exit_plan_mode exit_worktree "
    "get_goal glob grep_search image_gen list_agents list_directory loop_wakeup lsp manage_memory "
    "monitor notebook_edit omni_caption_audio omni_caption_image omni_clip_audio omni_clip_image "
    "omni_clip_video omni_convert_image omni_downsample_audio omni_downsample_image "
    "omni_downscale_video omni_extract_audio omni_extract_keyframes omni_ocr_image "
    "omni_recall_media_memory omni_transcribe_audio omni_understand_video_segments propose_goal "
    "read_file read_mcp_resource record_artifact record_source report_findings request_shutdown "
    "run_shell_command save_memory search_memory send_message skill structured_output task_create "
    "task_list task_stop task_update team_create team_delete team_plan_approval todo_write tool_call "
    "tool_search update_goal web_fetch web_search workflow write_file zoom_image"
).split()

FORBIDDEN = ("--bare", "--yolo", "-y", "yolo", "auto-edit", "--openai-api-key", "--system-prompt")
TOOLS_OFF = ("--approval-mode", "default", "--safe-mode", "--max-tool-calls", "0")
SID = "3f2a7c1e-9b4d-4e8f-a1c2-5d6e7f8a9b0c"


def turn(tmp_path: Path, system_prompt_file: str | None = "/h/.argus/prompts/p.md") -> TurnContext:
    return TurnContext(
        home=str(tmp_path),
        cwd=str(tmp_path / "w"),
        turn_dir=str(tmp_path / "t"),
        system_prompt_file=system_prompt_file,
    )


def build(tmp_path: Path, **kw):
    spec = InvokeSpec(user_id=USER_A, cli="qwen", prompt="hello", **kw)
    return build_command(spec, binaries=Binaries(), system_prompt_file=None, turn=turn(tmp_path))


def assert_safe(argv: list[str]) -> None:
    assert argv[0] == "qwen"
    assert argv[1:5] == ["--output-format", "stream-json", "--include-partial-messages", "--approval-mode"]
    joined = " ".join(argv)
    for flag in TOOLS_OFF:
        assert flag in argv
    assert "--approval-mode default" in joined
    assert "--max-tool-calls 0" in joined
    for bad in FORBIDDEN:
        assert bad not in argv


# ------------------------------------------------------------------------------------------- argv
def test_new_session_uses_the_id_argus_chose(tmp_path):
    cmd = build(tmp_path, session_id=SID)
    assert_safe(cmd.argv)
    assert cmd.argv[-2:] == ["--session-id", SID]
    assert "--resume" not in cmd.argv and "--chat-recording" not in cmd.argv
    assert cmd.stdin == "hello" and "hello" not in cmd.argv
    assert cmd.env == {"ENABLE_WEB_SEARCH": "false", "QWEN_SYSTEM_MD": "/h/.argus/prompts/p.md"}
    assert cmd.files == {} and cmd.result_file is None and cmd.protocol == "stdio"


def test_resume_and_model(tmp_path):
    cmd = build(tmp_path, session_id=SID, resume=True, model="qwen3-coder-plus")
    assert_safe(cmd.argv)
    assert cmd.argv[-4:] == ["-m", "qwen3-coder-plus", "--resume", SID]
    assert "--session-id" not in cmd.argv


def test_one_shot_turns_chat_recording_off(tmp_path):
    cmd = build(tmp_path, persist=False)
    assert_safe(cmd.argv)
    assert cmd.argv[-2:] == ["--chat-recording", "false"]
    assert "--session-id" not in cmd.argv and "--resume" not in cmd.argv


def test_no_system_prompt_no_env(tmp_path):
    spec = InvokeSpec(user_id=USER_A, cli="qwen", prompt="hello")
    cmd = plugin("qwen").build(spec, Binaries(qwen="/opt/qwen"), turn(tmp_path, None))
    assert cmd.argv[0] == "/opt/qwen"
    assert cmd.env == {"ENABLE_WEB_SEARCH": "false"}


def test_effort_is_never_passed(tmp_path):
    cmd = build(tmp_path, effort="high")
    assert not any("effort" in a for a in cmd.argv)
    assert plugin("qwen").supports_effort is False


def test_bad_values_never_reach_argv():
    with pytest.raises(ValueError):
        InvokeSpec(user_id=USER_A, cli="qwen", prompt="x", model="--yolo")
    with pytest.raises(ValueError):
        InvokeSpec(user_id=USER_A, cli="qwen", prompt="x", session_id="ses_abcdefghij")
    with pytest.raises(ValueError):
        InvokeSpec(user_id=USER_A, cli="qwen", prompt="x", session_id=SID, resume=True, persist=False)


# ---------------------------------------------------------------------------------------- key env
def test_dashscope_key_env(tmp_path):
    cmd = build(tmp_path, api_key="sk-dash-secret", key_provider="dashscope", model="qwen3-max")
    assert cmd.env["OPENAI_API_KEY"] == "sk-dash-secret"
    assert cmd.env["OPENAI_BASE_URL"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert cmd.env["OPENAI_MODEL"] == "qwen3-max"
    assert "sk-dash-secret" not in " ".join(cmd.argv)


def test_openrouter_key_env_and_default_models(tmp_path):
    cmd = build(tmp_path, api_key="sk-or-secret", key_provider="openrouter")
    assert cmd.env["OPENAI_API_KEY"] == "sk-or-secret"
    assert cmd.env["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert cmd.env["OPENAI_MODEL"] == "qwen/qwen3-coder-plus"  # qwen needs a model to pick the key path
    cmd = build(tmp_path, api_key="sk-dash-secret", key_provider="dashscope")
    assert cmd.env["OPENAI_MODEL"] == "qwen3-coder-plus"


def test_unknown_or_missing_provider_sets_no_key(tmp_path):
    assert key_env("qwen", "coding_plan", "k", None) == {}
    cmd = build(tmp_path, api_key="sk-x", key_provider="coding_plan")
    assert not any(k.startswith("OPENAI_") for k in cmd.env)
    cmd = build(tmp_path, api_key="sk-x")
    assert not any(k.startswith("OPENAI_") for k in cmd.env)
    assert set(plugin("qwen").key_providers) == {"dashscope", "openrouter"}


# ------------------------------------------------------------------------------ operator settings
def test_policy_file_denies_and_disables_every_tool():
    assert len(QWEN_TOOLS) == 71 and len(set(QWEN_TOOLS)) == 71
    data = json.loads(POLICY.read_text())
    assert data["$version"] == 4  # current schema: qwen never tries to migrate (rewrite) it
    deny = data["permissions"]["deny"]
    assert set(deny) == set(QWEN_TOOLS) | {"mcp__*"} and len(deny) == 72
    assert set(data["tools"]["disabled"]) == set(QWEN_TOOLS) and len(data["tools"]["disabled"]) == 71
    assert data["mcp"]["excluded"] == ["*"]
    assert data["tools"]["webSearch"]["enabled"] is False
    assert data["tools"]["approvalMode"] == "default"
    assert data["disableAllHooks"] is True
    assert data["security"]["auth"]["selectedType"] == "openai"
    assert data["privacy"]["usageStatisticsEnabled"] is False
    assert data["telemetry"]["enabled"] is False
    assert data["general"]["enableAutoUpdate"] is False


# ---------------------------------------------------------------------------- hooks on the output
def init_event(tools: list | None = None, sid: str = SID) -> dict:
    ev = {
        "type": "system",
        "subtype": "init",
        "uuid": sid,
        "session_id": sid,
        "cwd": "/w",
        "mcp_servers": [],
        "model": "qwen3-coder-plus",
        "permission_mode": "default",
        "slash_commands": [],
        "qwen_code_version": "0.27.1",
        "agents": [],
    }
    if tools is not None:
        ev["tools"] = tools
    return ev


def test_tools_reported_and_session_read_back():
    p = plugin("qwen")
    assert p.tools_reported(init_event([])) == []
    assert p.tools_reported(init_event(["read_file"])) == ["read_file"]
    assert p.tools_reported(init_event(None))  # no list at all: fail closed
    assert p.tools_reported({"type": "result"}) is None
    assert p.tools_reported({"type": "system", "subtype": "api_retry"}) is None
    assert p.session_from_event(init_event([])) == SID
    assert p.session_from_event({"type": "result", "session_id": SID}) is None


def test_no_in_app_sign_in_and_status_by_key():
    p = plugin("qwen")
    assert p.label == "Qwen Code"
    assert p.login is None and login_spec_for("qwen") is None
    assert logout_spec_for("qwen") == (None, ())
    assert p.status_args is None and p.status_files == ()


# ---------------------------------------------------------------------------------------- parser
def delta(kind: str, text: str) -> dict:
    key = "text" if kind == "text_delta" else "thinking"
    return {
        "type": "stream_event",
        "uuid": "u",
        "session_id": SID,
        "parent_tool_use_id": None,
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": kind, key: text}},
    }


def assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "uuid": "u2",
        "session_id": SID,
        "parent_tool_use_id": None,
        "message": {
            "id": "m",
            "type": "message",
            "role": "assistant",
            "model": "qwen3-coder-plus",
            "content": [{"type": "text", "text": text}],
            "stop_reason": None,
            "usage": {"input_tokens": 120, "output_tokens": 5},
        },
    }


def success(text: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "uuid": "u3",
        "session_id": SID,
        "is_error": False,
        "duration_ms": 900,
        "duration_api_ms": 800,
        "num_turns": 1,
        "result": text,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "total_tokens": 125,
        },
        "permission_denials": [],
    }


def error_result(message: str) -> dict:
    return {
        "type": "result",
        "subtype": "error_during_execution",
        "uuid": "u4",
        "session_id": SID,
        "is_error": True,
        "duration_ms": 10,
        "duration_api_ms": 0,
        "num_turns": 1,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "permission_denials": [],
        "error": {"message": message},
    }


def run(events: list[dict], exit_record: dict | None = None):
    parser = make_parser("qwen")
    out = []
    for ev in events:
        out += parser.feed(ev)
    return out, parser.finish(
        exit_record or {"type": "argus.exit", "ok": True, "exit_code": 0, "session_id": SID}
    )


def test_text_thinking_usage_and_session_without_duplicates():
    out, res = run(
        [
            init_event([]),
            delta("thinking_delta", "let me think"),
            delta("text_delta", "Hello"),
            delta("text_delta", " world"),
            assistant("Hello world"),
            success("Hello world"),
        ]
    )
    assert [(e.type, e.text) for e in out] == [
        ("thinking", "let me think"),
        ("text", "Hello"),
        ("text", " world"),
    ]
    assert res.text == "Hello world" and res.thinking == "let me think"
    assert res.session_id == SID and res.model == "qwen3-coder-plus"
    # promptTokenCount includes the cached tokens: counted once.
    assert (res.usage.input_tokens, res.usage.output_tokens, res.usage.cache_read_tokens) == (20, 5, 100)
    assert res.reported_cost_usd is None and res.failure is None


def test_without_partial_messages_the_assistant_text_is_used_once():
    out, res = run([init_event([]), assistant("Hi"), success("Hi")])
    assert [e.text for e in out] == ["Hi"] and res.text == "Hi"


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("[API Error: 429 Too Many Requests] Rate limit reached for requests", "rate_limit"),
        ("Throttling.RateQuota: Requests rate limit exceeded", "rate_limit"),
        ("[API Error: 401 Incorrect API key provided.]", "auth"),
        ("Your login credentials are no longer valid. Run /auth to sign in again.", "auth"),
        ("Qwen OAuth free tier was discontinued on 2026-04-15.", "auth"),
        ("Arrearage: Access denied, please make sure your account is in good standing.", "quota"),
        ("[API Error: 429 You exceeded your current quota (insufficient_quota)]", "quota"),
        ("[API Error: 400 model `qwen9` does not exist]", "invalid"),
    ],
)
def test_error_results_map_to_fallback_categories(message, category):
    _, res = run(
        [init_event([]), error_result(message)],
        {"type": "argus.exit", "ok": False, "exit_code": 1, "error": "exit_status", "detail": ""},
    )
    assert res.failure is not None and res.failure.category == category
    assert message[:40] in res.failure.message


def test_exit_41_is_auth_even_without_a_result():
    _, res = run(
        [],
        {
            "type": "argus.exit",
            "ok": False,
            "exit_code": 41,
            "error": "exit_status",
            "detail": "No auth type is selected. Please configure an auth type",
        },
    )
    assert res.failure.category == "auth"


def test_exit_41_upgrades_a_generic_error_to_auth():
    _, res = run(
        [init_event([]), error_result("Something went wrong")],
        {"type": "argus.exit", "ok": False, "exit_code": 41, "error": "exit_status", "detail": ""},
    )
    assert res.failure.category == "auth"


@pytest.mark.parametrize("code", [53, 55])
def test_argus_own_limits_do_not_fall_back(code):
    _, res = run(
        [init_event([]), delta("text_delta", "partial")],
        {"type": "argus.exit", "ok": False, "exit_code": code, "error": "exit_status", "detail": ""},
    )
    assert res.failure.category == "error"
    assert res.text == "partial"


def test_exit_detail_with_a_limit_message_falls_back():
    _, res = run(
        [],
        {
            "type": "argus.exit",
            "ok": False,
            "exit_code": 1,
            "error": "exit_status",
            "detail": "429 Too Many Requests",
        },
    )
    assert res.failure.category == "rate_limit"


def test_runner_failures_keep_their_category():
    _, res = run(
        [],
        {"type": "argus.exit", "ok": False, "exit_code": None, "error": "timeout", "detail": "Throttling"},
    )
    assert res.failure.category == "unavailable"
    _, res = run(
        [],
        {"type": "argus.exit", "ok": False, "exit_code": -9, "error": "tools_not_disabled", "detail": "x"},
    )
    assert res.failure.category == "error"


# --------------------------------------------------------------------------- through the runner
FAKE_QWEN = r"""
import json, os, sys
prompt = sys.stdin.read()
argv = sys.argv[1:]
assert "--bare" not in argv and "--yolo" not in argv
sid = None
for flag in ("--session-id", "--resume"):
    if flag in argv:
        sid = argv[argv.index(flag) + 1]
sid = sid or "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
if prompt == "auth":
    sys.stderr.write("Error: OPENAI_API_KEY environment variable not found.\n")
    sys.exit(41)
system_md = os.environ.get("QWEN_SYSTEM_MD")
tools = ["read_file", "run_shell_command"] if prompt == "tools" else []
print(json.dumps({"type": "system", "subtype": "init", "uuid": sid, "session_id": sid, "cwd": os.getcwd(),
                  "tools": tools, "mcp_servers": [], "model": os.environ.get("OPENAI_MODEL", "x"),
                  "permission_mode": "default", "slash_commands": [], "qwen_code_version": "0.27.1"}))
report = {"argv": argv, "system": open(system_md).read() if system_md else None,
          "env": sorted(os.environ), "web": os.environ.get("ENABLE_WEB_SEARCH")}
ev = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": json.dumps(report)}}
print(json.dumps({"type": "stream_event", "uuid": "u", "session_id": sid, "parent_tool_use_id": None, "event": ev}))
print(json.dumps({"type": "result", "subtype": "success", "uuid": "r", "session_id": sid, "is_error": False,
                  "result": json.dumps(report), "usage": {"input_tokens": 3, "output_tokens": 1}}))
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    bins = tmp_path / "bin"
    bins.mkdir()
    cfg = RunnerConfig(home_root=tmp_path / "users", binaries=Binaries(qwen=_exe(bins / "qwen", FAKE_QWEN)))
    return Runner(cfg)


async def collect(runner: Runner, **kw) -> list[dict]:
    return [e async for e in runner.invoke(InvokeSpec(user_id=USER_A, cli="qwen", **kw))]


async def test_turn_through_the_runner(runner):
    sid = str(uuid.uuid4())
    evs = await collect(
        runner,
        prompt="hi",
        system_prompt="You are a council seat.",
        session_id=sid,
        api_key="sk-dash-supersecret",
        key_provider="dashscope",
    )
    assert evs[-1]["type"] == "argus.exit" and evs[-1]["ok"] and evs[-1]["session_id"] == sid
    parser = make_parser("qwen")
    for ev in evs[:-1]:
        parser.feed(ev)
    res = parser.finish(evs[-1])
    report = json.loads(res.text)
    assert report["system"] == "You are a council seat."
    assert report["web"] == "false"
    assert "OPENAI_API_KEY" in report["env"] and "ARGUS_RUNNER_TOKEN" not in report["env"]
    assert report["argv"][-2:] == ["--session-id", sid]
    assert res.model == "qwen3-coder-plus" and res.session_id == sid
    assert runner.has_session(USER_A, "qwen", sid)


async def test_a_session_qwen_picked_is_read_back(runner):
    evs = await collect(runner, prompt="hi")
    assert evs[-1]["ok"] and evs[-1]["session_id"] == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


async def test_reported_tools_kill_the_turn(runner):
    evs = await collect(runner, prompt="tools", session_id=str(uuid.uuid4()))
    assert [e["type"] for e in evs] == ["argus.exit"]
    assert evs[-1]["ok"] is False and evs[-1]["error"] == "tools_not_disabled"


async def test_exit_41_through_the_runner_is_auth(runner):
    evs = await collect(runner, prompt="auth", persist=False)
    parser = make_parser("qwen")
    res = parser.finish(evs[-1])
    assert evs[-1]["exit_code"] == 41 and res.failure.category == "auth"


async def test_runner_sign_in_status_and_sign_out(runner):
    with pytest.raises(LoginError) as exc:
        await runner.logins.start(USER_A, "qwen")
    assert exc.value.code == "not_supported"
    status = await runner.status(USER_A, "qwen")
    assert status == {"logged_in": None, "method": None, "verified": False, "detail": "Uses an API key."}
    out = await runner.logout(USER_A, "qwen")
    assert out["ok"] is True
    await runner.logins.close()
