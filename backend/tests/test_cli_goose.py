"""Goose (``goose run`` v1.57.0): argv, env per key provider, the stream-json parser and a runner turn.

Goose is never run here: a fake executable prints the events Goose's source writes
(crates/goose-cli/src/session/mod.rs, ``StreamEvent``).
"""

from __future__ import annotations

import json
import stat
import sys
import uuid
from pathlib import Path

import pytest

from argus.providers.cli.parsers import make_parser
from argus.providers.cli.vendors.goose import MAX_TURNS_MESSAGE, Parser
from argus.runner.clis import key_env, login_spec_for, logout_spec_for, plugin
from argus.runner.clis.goose import CLI_WRAPPING_PROVIDERS, KEY_PROVIDERS, PLUGIN
from argus.runner.process import Runner, RunnerConfig
from argus.runner.specs import Binaries, InvokeSpec, TurnContext, build_command

USER = "11111111-1111-4111-8111-111111111111"
SID = "0f6a3c1e-2b4d-4e8f-9a1b-3c5d7e9f1a2b"
HOME = "/srv/argus/users/u1"
TURN = TurnContext(
    home=HOME, cwd=f"{HOME}/work/r1", turn_dir=f"{HOME}/.argus/turns/t1", system_prompt_file=None
)
KEY = "sk-test-goose-key-123"
# Flags that would give Goose tools, ask for input, or add to the system prompt through argv.
NEVER = (
    "--with-extension",
    "--with-streamable-http-extension",
    "--with-builtin",
    "--recipe",
    "-s",
    "--interactive",
    "--system",
    "-t",
    "--text",
    "--provider",
)


def _spec(**kw) -> InvokeSpec:
    base = {
        "user_id": USER,
        "cli": "goose",
        "prompt": "the prompt",
        "api_key": KEY,
        "key_provider": "anthropic",
    }
    return InvokeSpec(**(base | kw))


def _build(turn: TurnContext = TURN, **kw):
    return build_command(
        _spec(**kw), binaries=Binaries(), system_prompt_file=turn.system_prompt_file, turn=turn
    )


# ------------------------------------------------------------------------------------------- argv


def _assert_tools_off(argv: list[str]) -> None:
    assert argv[:7] == ["goose", "run", "--no-profile", "--output-format", "stream-json", "-i", "-"]
    assert argv[argv.index("--max-turns") + 1] == "4"
    for flag in NEVER:
        assert flag not in argv


def test_new_session_is_named_with_the_id_argus_chose():
    cmd = _build(session_id=SID)
    _assert_tools_off(cmd.argv)
    assert cmd.argv[-2:] == ["-n", SID]
    assert "--resume" not in cmd.argv and "--no-session" not in cmd.argv
    assert cmd.stdin == "the prompt" and "the prompt" not in cmd.argv


def test_resume_finds_the_session_by_its_name():
    cmd = _build(session_id=SID, resume=True)
    _assert_tools_off(cmd.argv)
    assert cmd.argv[-3:] == ["--resume", "-n", SID]
    assert "--session-id" not in cmd.argv  # Goose's own ids are YYYYMMDD_<n>, never ours


def test_one_shot_stores_nothing():
    cmd = _build(persist=False)
    _assert_tools_off(cmd.argv)
    assert cmd.argv[-1] == "--no-session" and "-n" not in cmd.argv
    # Without an id to find it by later, nothing is stored either.
    assert _build().argv[-1] == "--no-session"


def test_model_and_prompt_never_reach_argv():
    cmd = _build(session_id=SID, model="claude-sonnet-4-5")
    assert "claude-sonnet-4-5" not in cmd.argv and "--model" not in cmd.argv
    assert cmd.env["GOOSE_MODEL"] == "claude-sonnet-4-5"


# -------------------------------------------------------------------------------------------- env


def test_env_turns_tools_off_and_keeps_goose_in_the_users_home():
    cmd = _build(session_id=SID)
    assert cmd.env == {
        "ANTHROPIC_API_KEY": KEY,
        "GOOSE_PROVIDER": "anthropic",
        "GOOSE_MODE": "chat",
        "GOOSE_PATH_ROOT": f"{HOME}/.goose",
        "GOOSE_DISABLE_KEYRING": "1",
        "GOOSE_TELEMETRY_ENABLED": "false",
        "GOOSE_DISABLE_SESSION_NAMING": "true",
    }


def test_system_prompt_goes_through_a_file():
    turn = TurnContext(
        home=HOME, cwd=TURN.cwd, turn_dir=TURN.turn_dir, system_prompt_file=f"{HOME}/.argus/p.md"
    )
    cmd = _build(turn, session_id=SID, system_prompt="be terse")
    assert cmd.env["GOOSE_SYSTEM_PROMPT_FILE_PATH"] == f"{HOME}/.argus/p.md"
    assert "be terse" not in cmd.argv and "be terse" not in cmd.stdin


@pytest.mark.parametrize(
    ("provider", "key_var", "goose_provider"),
    [
        ("anthropic", "ANTHROPIC_API_KEY", "anthropic"),
        ("openai", "OPENAI_API_KEY", "openai"),
        ("google", "GOOGLE_API_KEY", "google"),
        ("openrouter", "OPENROUTER_API_KEY", "openrouter"),
        ("mistral", "MISTRAL_API_KEY", "mistral"),
    ],
)
def test_key_env_per_provider(provider, key_var, goose_provider):
    env = _build(key_provider=provider, model="some-model").env
    assert (
        env[key_var] == KEY and env["GOOSE_PROVIDER"] == goose_provider and env["GOOSE_MODEL"] == "some-model"
    )
    assert [k for k in env if k.endswith("_API_KEY")] == [key_var]
    # No model: GOOSE_MODEL is left out (Goose uses the provider's default), never set empty.
    assert "GOOSE_MODEL" not in _build(key_provider=provider).env
    assert key_env("goose", provider, KEY, None)[key_var] == KEY


def test_no_key_provider_runs_another_vendors_agent():
    assert set(KEY_PROVIDERS) == {"anthropic", "openai", "google", "openrouter", "mistral"}
    wrapping = {
        "claude-code",
        "codex",
        "gemini-cli",
        "cursor-agent",
        "claude-acp",
        "codex-acp",
        "copilot-acp",
        "amp-acp",
        "pi-acp",
    }
    assert wrapping <= CLI_WRAPPING_PROVIDERS
    for template in PLUGIN.key_providers.values():
        provider = template["GOOSE_PROVIDER"]
        assert provider not in CLI_WRAPPING_PROVIDERS
        assert not provider.endswith("-acp") and not provider.endswith("_acp")
        assert set(template) <= {"GOOSE_PROVIDER", "GOOSE_MODEL"} | {
            k for k in template if k.endswith("_API_KEY")
        }


def test_a_turn_without_a_usable_key_is_refused():
    # Goose would otherwise take its provider from its own config file.
    with pytest.raises(ValueError):
        _build(api_key=None)
    with pytest.raises(ValueError):
        _build(key_provider="claude-code".replace("-", "_"))
    with pytest.raises(ValueError):
        _build(key_provider=None)


def test_the_key_template_cannot_override_the_tools_off_env(monkeypatch):
    monkeypatch.setitem(KEY_PROVIDERS, "anthropic", KEY_PROVIDERS["anthropic"] | {"GOOSE_MODE": "auto"})
    assert _build().env["GOOSE_MODE"] == "chat"


def test_plugin_has_no_sign_in_and_no_hooks():
    assert plugin("goose") is PLUGIN and PLUGIN.label == "Goose" and not PLUGIN.supports_effort
    assert login_spec_for("goose") is None and logout_spec_for("goose") == (None, ())
    assert PLUGIN.status_args is None and PLUGIN.status_files == ()
    for event in (
        {"type": "message", "message": {"id": "m", "role": "assistant", "content": []}},
        {"type": "complete", "total_tokens": 3},
    ):
        assert PLUGIN.session_from_event(event) is None
        assert PLUGIN.tools_reported(event) is None


# ----------------------------------------------------------------------------------------- parser


def _msg(
    text: str | None = None, *, id: str = "msg_1", role: str = "assistant", blocks=None, model=None
) -> dict:
    content = list(blocks or [])
    if text is not None:
        content.append({"type": "text", "text": text})
    metadata: dict = {"userVisible": True, "agentVisible": True}
    if model:
        metadata["inference"] = {"provider": "anthropic", "requestedModel": "x", "resolvedModel": model}
    return {
        "type": "message",
        "message": {"id": id, "role": role, "created": 1, "content": content, "metadata": metadata},
    }


def _run(events: list[dict], exit_record: dict | None = None):
    parser = make_parser("goose")
    assert isinstance(parser, Parser)
    out = [e for ev in events for e in parser.feed(ev)]
    return out, parser.finish(exit_record or {"type": "argus.exit", "ok": True, "session_id": SID})


def test_chunks_sharing_an_id_are_streamed_once():
    events = [
        _msg("the prompt", id="u1", role="user"),
        _msg("Hel", model="claude-sonnet-4-5-20250929"),
        _msg("lo, "),
        _msg("world"),
        {
            "type": "complete",
            "total_tokens": 150,
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_read_input_tokens": 80,
            "cache_write_input_tokens": 10,
            "cost_usd": 0.0123,
        },
    ]
    out, result = _run(events)
    assert [(e.type, e.text) for e in out] == [("text", "Hel"), ("text", "lo, "), ("text", "world")]
    assert result.text == "Hello, world" and result.failure is None
    assert result.session_id == SID  # from the runner: Goose never prints it
    assert result.model == "claude-sonnet-4-5-20250929"
    assert result.usage.input_tokens == 30 and result.usage.output_tokens == 30
    assert result.usage.cache_read_tokens == 80 and result.usage.cache_write_tokens == 10
    assert result.reported_cost_usd == pytest.approx(0.0123)


def test_a_repeat_of_the_whole_reply_is_not_doubled():
    out, result = _run([_msg("ab"), _msg("cd"), _msg("abcd")])
    assert "".join(e.text for e in out) == "abcd" and result.text == "abcd"


def test_complete_without_optional_fields():
    _, result = _run([_msg("hi"), {"type": "complete", "total_tokens": None}])
    assert result.usage.input_tokens == 0 and result.reported_cost_usd is None


def test_thinking_is_kept_apart_from_text():
    out, result = _run(
        [
            _msg(
                "answer",
                blocks=[
                    {"type": "thinking", "thinking": "hmm", "signature": "s"},
                    {"type": "redactedThinking", "data": "xx"},
                ],
            )
        ]
    )
    assert [(e.type, e.text) for e in out] == [("thinking", "hmm"), ("text", "answer")]
    assert result.thinking == "hmm" and result.text == "answer"


def test_tool_requests_and_notifications_are_not_text():
    tool = {"type": "toolRequest", "id": "t1", "toolCall": {"status": "success", "value": {"name": "x"}}}
    out, result = _run(
        [
            _msg(None, blocks=[tool]),
            {"type": "notification", "extension_id": "e", "log": {"message": "hi"}},
            _msg("done"),
        ]
    )
    assert result.text == "done" and [e.text for e in out] == ["done"]


def test_the_turn_limit_message_is_not_part_of_the_answer():
    out, result = _run([_msg("real answer"), _msg(MAX_TURNS_MESSAGE, id="msg_2")])
    assert result.text == "real answer" and [e.text for e in out] == ["real answer"]
    assert result.warnings == ["goose stopped at its turn limit"]


@pytest.mark.parametrize(
    ("error", "category"),
    [
        ("Rate limit exceeded: 429 Too Many Requests", "rate_limit"),
        (
            "Ran into this error: Rate limit exceeded: slow down.\n\nPlease retry if you think this is a transient "
            "or recoverable error.",
            "rate_limit",
        ),
        ("Credits exhausted: your balance is zero", "quota"),
        ("Authentication error: invalid x-api-key", "auth"),
        ("Ran into this error: Authentication error: bad key.\n\nPlease retry", "auth"),
        ("Provider is not configured", "auth"),
        ("Server error: 503", "unavailable"),
        ("Context length exceeded: too long", "invalid"),
        ("something odd", "error"),
    ],
)
def test_terminal_error_categories(error, category):
    _, result = _run(
        [{"type": "error", "error": error}],
        {"type": "argus.exit", "ok": False, "error": "exit_status", "detail": ""},
    )
    assert result.failure is not None and result.failure.category == category


def test_error_block_kind_sets_the_category():
    block = {
        "type": "error",
        "kind": "authentication",
        "message": "Ran into this error: Authentication error: nope",
    }
    _, result = _run(
        [_msg(None, blocks=[block]), {"type": "error", "error": block["message"]}],
        {"type": "argus.exit", "ok": False, "error": "exit_status", "detail": ""},
    )
    assert result.failure.category == "auth" and result.text == ""
    other = {"type": "error", "kind": "other", "message": "Ran into this error: Rate limit exceeded: wait"}
    _, result = _run([_msg(None, blocks=[other])])
    assert result.failure.category == "rate_limit"


def test_credits_exhausted_notification_is_a_quota_failure_even_when_goose_completes():
    note = {
        "type": "systemNotification",
        "notificationType": "creditsExhausted",
        "msg": "Please add credits to your account, then resend your message to continue.",
        "data": {"top_up_url": None},
    }
    _, result = _run([_msg(None, blocks=[note]), {"type": "complete", "total_tokens": 0}])
    assert result.failure is not None and result.failure.category == "quota"


def test_runner_failure_without_events():
    _, result = _run([], {"type": "argus.exit", "ok": False, "error": "timeout", "detail": "timed out"})
    assert result.failure.category == "unavailable"


# ---------------------------------------------------------------------------- runner, fake goose

FAKE_GOOSE = """
import json, os, sys
prompt = sys.stdin.read()
argv = sys.argv[1:]
keys = ("GOOSE_MODE", "GOOSE_PATH_ROOT", "GOOSE_PROVIDER", "GOOSE_MODEL", "GOOSE_DISABLE_KEYRING",
        "GOOSE_TELEMETRY_ENABLED", "GOOSE_DISABLE_SESSION_NAMING", "GOOSE_SYSTEM_PROMPT_FILE_PATH")
seen = {"argv": argv, "stdin": prompt, "env": {k: os.environ.get(k) for k in keys},
        "has_key": os.environ.get("ANTHROPIC_API_KEY") == "sk-test-goose-key-123"}
path = os.environ.get("GOOSE_SYSTEM_PROMPT_FILE_PATH")
if path:
    seen["system"] = open(path).read()
print(json.dumps({"type": "notification", "extension_id": "argus-test", "log": {"message": json.dumps(seen)}}))
print(json.dumps({"type": "message", "message": {"id": None, "role": "user", "created": 1,
                  "content": [{"type": "text", "text": prompt}], "metadata": {}}}))
for chunk in ("Hel", "lo"):
    print(json.dumps({"type": "message", "message": {"id": "msg_x", "role": "assistant", "created": 1,
                      "content": [{"type": "text", "text": chunk}], "metadata": {}}}))
if "fail" in prompt:
    print(json.dumps({"type": "error", "error": "Rate limit exceeded: 429"}))
    sys.exit(1)
print(json.dumps({"type": "complete", "total_tokens": 7, "input_tokens": 5, "output_tokens": 2}))
"""


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    exe = tmp_path / "goose"
    exe.write_text(f"#!{sys.executable}\n{FAKE_GOOSE}")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return Runner(RunnerConfig(home_root=tmp_path / "users", binaries=Binaries(goose=str(exe))))


async def _turn(runner: Runner, **kw) -> tuple[dict, list[dict]]:
    events = [e async for e in runner.invoke(_spec(**kw))]
    seen = json.loads(events[0]["log"]["message"])
    return seen, events


async def test_runner_turn_creates_then_resumes_the_named_session(runner):
    sid = str(uuid.uuid4())
    seen, events = await _turn(runner, session_id=sid, run_key="r1", system_prompt="be terse")
    home = runner.cfg.home_root / USER
    assert seen["argv"][-2:] == ["-n", sid] and seen["stdin"] == "the prompt" and seen["has_key"]
    assert seen["env"]["GOOSE_MODE"] == "chat" and seen["env"]["GOOSE_PATH_ROOT"] == str(home / ".goose")
    assert seen["env"]["GOOSE_PROVIDER"] == "anthropic" and seen["env"]["GOOSE_MODEL"] is None
    assert seen["system"] == "be terse"
    exit_ = events[-1]
    assert exit_["ok"] and exit_["session_id"] == sid and runner.has_session(USER, "goose", sid)

    parser = make_parser("goose")
    for event in events[:-1]:
        parser.feed(event)
    result = parser.finish(exit_)
    assert result.text == "Hello" and result.session_id == sid and result.usage.input_tokens == 5

    seen, events = await _turn(runner, session_id=sid, resume=True)
    assert seen["argv"][-3:] == ["--resume", "-n", sid] and events[-1]["ok"]


async def test_runner_turn_rate_limited(runner):
    _, events = await _turn(runner, prompt="please fail", persist=False)
    parser = make_parser("goose")
    for event in events[:-1]:
        parser.feed(event)
    result = parser.finish(events[-1])
    assert result.failure is not None and result.failure.category == "rate_limit"
    assert result.session_id is None and runner.sessions(USER, "goose") == []


async def test_runner_refuses_a_turn_without_a_key(runner):
    events = [e async for e in runner.invoke(_spec(api_key=None))]
    assert events == [
        {
            "type": "argus.exit",
            "ok": False,
            "error": "invalid_request",
            "detail": "goose needs an API key and its provider",
        }
    ]
