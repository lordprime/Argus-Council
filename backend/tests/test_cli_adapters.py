"""CLI agent adapters: event parsing, argv safety, sessions, tool calls, billing and errors."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus.providers.cli.errors import CLIError
from argus.providers.cli.parsers import make_parser
from argus.providers.cli.provider import CLIProvider
from argus.providers.cli.toolproto import OPEN, extract_tool_calls, visible_prefix
from argus.providers.types import CompletionRequest, Message, ToolSpec
from argus.runner.specs import CODEX_DISABLED_FEATURES, Binaries, InvokeSpec, build_command

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
USER = "7d6c4f0e-3b1a-4c8e-9f2d-5a6b7c8d9e0f"
OK_EXIT = {"type": "argus.exit", "ok": True, "exit_code": 0}


def events(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def parse(cli: str, name: str, exit_record: dict | None = None):
    parser = make_parser(cli)
    streamed = [e for ev in events(name) for e in parser.feed(ev)]
    return streamed, parser.finish(exit_record or OK_EXIT)


# ---------------------------------------------------------------- parsers
def test_claude_stream_json_success():
    streamed, result = parse("claude", "claude_success.jsonl")
    assert [e.type for e in streamed] == ["thinking", "text", "text"]
    assert "".join(e.text for e in streamed if e.type == "text") == "Solid-state cells look promising."
    assert result.text == "Solid-state cells look promising."
    assert result.thinking == "Weigh energy density against cost."
    assert result.session_id == "6f1c2a9e-8b7d-4e3f-a1b2-c3d4e5f60718"
    assert result.model == "claude-sonnet-5-5"
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cache_read_tokens) == (
        1200,
        42,
        800,
    )
    assert result.reported_cost_usd == pytest.approx(0.0123)
    assert result.failure is None


def test_claude_auth_failure_recorded_from_real_output():
    streamed, result = parse("claude", "claude_auth_failed.jsonl", {"type": "argus.exit", "ok": False})
    assert streamed == []  # the synthetic error text never reaches the thought stream
    assert result.failure is not None and result.failure.category == "auth"
    assert "Invalid API key" in result.failure.message


def test_claude_usage_limit_is_fallback_worthy():
    _, result = parse("claude", "claude_usage_limit.jsonl", {"type": "argus.exit", "ok": False})
    assert result.failure is not None
    assert CLIError("x", provider="claude-cli", category=result.failure.category).fallback


def test_codex_exec_json_success_with_tool_call():
    streamed, result = parse("codex", "codex_success.jsonl")
    assert [e.type for e in streamed] == ["thinking", "text"]
    assert result.session_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    # input_tokens includes cached tokens: split them so cost accounting is not double counted
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.output_tokens) == (
        315,
        24448,
        122,
    )
    text, calls = extract_tool_calls(result.text, {"notes_write"})
    assert text == "I will record the finding."
    assert calls[0].name == "notes_write" and calls[0].arguments == {"text": "Energy density 400 Wh/kg"}
    assert result.warnings and "Model metadata" in result.warnings[0]


@pytest.mark.parametrize(
    ("name", "category"),
    [("codex_auth_failed.jsonl", "auth"), ("codex_usage_limit.jsonl", "quota")],
)
def test_codex_failures(name, category):
    _, result = parse("codex", name, {"type": "argus.exit", "ok": False, "exit_code": 1})
    assert result.failure is not None and result.failure.category == category


def test_gemini_stream_json_success():
    streamed, result = parse("gemini", "gemini_success.jsonl")
    assert "".join(e.text for e in streamed) == "The evidence is mixed."
    assert result.session_id == "3e4f5a6b-7c8d-4e9f-a0b1-c2d3e4f5a6b7"
    assert result.model == "gemini-2.5-pro"
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.output_tokens) == (
        100,
        20,
        30,
    )


def test_gemini_quota_error():
    _, result = parse("gemini", "gemini_quota.jsonl", {"type": "argus.exit", "ok": False, "exit_code": 1})
    assert result.failure is not None and result.failure.category == "quota"


@pytest.mark.parametrize(
    ("error", "category"),
    [("not_installed", "unavailable"), ("timeout", "unavailable"), ("unknown_session", "session")],
)
def test_runner_exit_errors_are_classified(error, category):
    parser = make_parser("claude")
    result = parser.finish({"type": "argus.exit", "ok": False, "error": error, "detail": ""})
    assert result.failure is not None and result.failure.category == category


def test_stream_that_ends_without_exit_record_is_a_failure():
    parser = make_parser("gemini")
    result = parser.finish(None)
    assert result.failure is not None and result.failure.category == "unavailable"


# ---------------------------------------------------------------- argv safety
BINS = Binaries(claude="/opt/clis/claude", codex="/opt/clis/codex", gemini="/opt/clis/gemini")


def spec(cli: str, **kw) -> InvokeSpec:
    return InvokeSpec(user_id=USER, cli=cli, prompt="SECRET PROMPT TEXT", **kw)


def test_claude_argv_disables_every_builtin_tool():
    cmd = build_command(
        spec("claude", model="sonnet", session_id=str(uuid.uuid4()), system_prompt="be terse"),
        binaries=BINS,
        system_prompt_file="/tmp/p.md",
    )
    argv = cmd.argv
    assert argv[:2] == ["/opt/clis/claude", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--disallowedTools") + 1] == "*"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert "--disable-slash-commands" in argv
    assert "--session-id" in argv and "--resume" not in argv
    assert argv[argv.index("--system-prompt-file") + 1] == "/tmp/p.md"
    assert not any("dangerously" in a or a == "bypassPermissions" for a in argv)
    assert "SECRET PROMPT TEXT" not in argv and cmd.stdin == "SECRET PROMPT TEXT"
    assert "--bare" not in argv  # subscription login: --bare would ignore it


def test_claude_with_api_key_runs_bare_and_key_only_in_env():
    cmd = build_command(
        spec("claude", api_key="sk-ant-test-1234567890"), binaries=BINS, system_prompt_file=None
    )
    assert "--bare" in cmd.argv
    assert cmd.env == {"ANTHROPIC_API_KEY": "sk-ant-test-1234567890"}
    assert not any("sk-ant" in a for a in cmd.argv)


def test_claude_resume_and_one_shot():
    sid = str(uuid.uuid4())
    resumed = build_command(
        spec("claude", session_id=sid, resume=True), binaries=BINS, system_prompt_file=None
    )
    assert resumed.argv[resumed.argv.index("--resume") + 1] == sid
    oneshot = build_command(spec("claude", persist=False), binaries=BINS, system_prompt_file=None)
    assert "--no-session-persistence" in oneshot.argv and "--session-id" not in oneshot.argv


def test_codex_argv_read_only_without_tools():
    cmd = build_command(
        spec("codex", model="gpt-5.5-codex", api_key="sk-proj-abcdef123456"),
        binaries=BINS,
        system_prompt_file=None,
    )
    argv = cmd.argv
    assert argv[:3] == ["/opt/clis/codex", "exec", "--json"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "approval_policy=never" in argv and "web_search=disabled" in argv
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert disabled == set(CODEX_DISABLED_FEATURES) and {"shell_tool", "unified_exec"} <= disabled
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert not any("dangerously" in a or a in ("--full-auto", "--approve-for-me") for a in argv)
    assert argv[-1] == "-"  # prompt from stdin
    assert cmd.env == {"CODEX_API_KEY": "sk-proj-abcdef123456"}


def test_codex_resume_puts_options_before_subcommand():
    sid = str(uuid.uuid4())
    cmd = build_command(spec("codex", session_id=sid, resume=True), binaries=BINS, system_prompt_file=None)
    assert cmd.argv[-3:] == ["resume", sid, "-"]
    assert cmd.argv.index("--sandbox") < cmd.argv.index("resume")


def test_gemini_argv_never_yolo():
    cmd = build_command(
        spec("gemini", model="gemini-2.5-pro", session_id=str(uuid.uuid4()), system_prompt="x"),
        binaries=BINS,
        system_prompt_file="/home/u/.argus/prompts/a.md",
    )
    assert "--yolo" not in cmd.argv and "-y" not in cmd.argv
    assert cmd.argv[cmd.argv.index("--approval-mode") + 1] == "default"
    assert cmd.argv[cmd.argv.index("--output-format") + 1] == "stream-json"
    assert cmd.env["GEMINI_SYSTEM_MD"] == "/home/u/.argus/prompts/a.md"
    assert "SECRET PROMPT TEXT" not in cmd.argv


@pytest.mark.parametrize(
    "bad",
    [
        {"model": "--dangerously-skip-permissions"},
        {"model": "sonnet; rm -rf /"},
        {"model": "a b"},
        {"session_id": "../../other-user"},
        {"session_id": "--resume"},
        {"run_key": "../escape"},
        {"user_id": "../../etc"},
        {"session_id": str(uuid.uuid4()), "resume": True, "persist": False},
    ],
)
def test_invoke_spec_rejects_injection(bad):
    data = {"user_id": USER, "cli": "claude", "prompt": "hi", **bad}
    with pytest.raises(ValidationError):
        InvokeSpec(**data)


def test_codex_spec_cannot_choose_new_session_id():
    with pytest.raises(ValidationError):
        InvokeSpec(user_id=USER, cli="codex", prompt="hi", session_id=str(uuid.uuid4()))


def test_tool_call_extraction_rejects_unknown_and_malformed():
    text = (
        f'a {OPEN}{{"name": "shell", "arguments": {{}}}}</argus_tool_call> b {OPEN}not json</argus_tool_call>'
    )
    cleaned, calls = extract_tool_calls(text, {"notes_write"})
    assert calls == [] and "shell" in cleaned


def test_visible_prefix_hides_partial_tag():
    assert visible_prefix("hello <argus_to") == len("hello ")
    assert visible_prefix(f"hello {OPEN}{{") == len("hello ")
    assert visible_prefix("plain <b>") == len("plain <b>")


# ---------------------------------------------------------------- provider
class FakeTransport:
    def __init__(self, scripts: list[list[dict]]) -> None:
        self.scripts = scripts
        self.calls: list[InvokeSpec] = []
        self.sessions: set[tuple[str, str, str]] = set()

    async def invoke(self, spec: InvokeSpec):
        self.calls.append(spec)
        script = self.scripts.pop(0)
        for event in script(spec) if callable(script) else script:
            yield event

    async def has_session(self, user_id: str, cli: str, session_id: str) -> bool:
        return (user_id, cli, session_id) in self.sessions


class FakeBackend:
    allow_subscription = True
    timeout_s = 600

    def __init__(self, transport: FakeTransport, key: str | None = None) -> None:
        self.transport = transport
        self.key = key
        self.owned: set[tuple[uuid.UUID, str, str]] = set()
        self.remembered: list[tuple] = []

    async def api_key(self, owner_id, cli):
        return self.key

    async def session_owned(self, owner_id, cli, session_id):
        return (owner_id, cli, session_id) in self.owned

    async def remember_session(self, owner_id, cli, session_id, run_id, member_id):
        self.remembered.append((owner_id, cli, session_id, run_id, member_id))


def claude_turn(text: str, sid: str, cost: float) -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "session_id": sid, "model": "claude-sonnet-5-5"},
        {
            "type": "assistant",
            "message": {"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": text}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": sid,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
        {"type": "argus.exit", "ok": True, "exit_code": 0, "session_id": sid},
    ]


def request(messages, **meta) -> CompletionRequest:
    return CompletionRequest(
        model="sonnet",
        messages=messages,
        tools=[ToolSpec(name="notes_write", description="Write a note")],
        metadata=meta,
    )


async def test_seat_keeps_its_session_and_sends_only_the_delta():
    owner = uuid.UUID(USER)
    call = f'{OPEN}{{"name": "notes_write", "arguments": {{"text": "n"}}}}</argus_tool_call>'
    # The CLI echoes the session id Argus chose (claude --session-id / --resume).
    transport = FakeTransport(
        [
            lambda s: claude_turn("Noted. " + call, s.session_id, 0.01),
            lambda s: claude_turn("All done.", s.session_id, 0.025),
        ]
    )
    backend = FakeBackend(transport)
    provider = CLIProvider("claude", owner, backend)
    meta = {"run_id": "run-1", "member_id": "seat-1"}
    msgs = [Message.system("You are the analyst."), Message.user("Task: analyse")]

    first = await provider.complete(request(msgs, **meta))
    assert first.tool_calls and first.tool_calls[0].name == "notes_write"
    assert first.content == "Noted." and first.finish_reason == "tool_calls"
    assert first.billing == "subscription" and first.usage.cost_usd == 0
    assert first.estimated_cost_usd == pytest.approx(0.01)
    spec1 = transport.calls[0]
    assert spec1.resume is False and spec1.session_id and spec1.persist
    assert "notes_write" in spec1.system_prompt and "You are the analyst." in spec1.system_prompt
    assert spec1.user_id == USER and spec1.api_key is None

    msgs += [first.as_message(), Message.tool_result(first.tool_calls[0].id, "saved")]
    second = await provider.complete(request(msgs, **meta))
    spec2 = transport.calls[1]
    assert spec2.resume is True and spec2.session_id == spec1.session_id
    assert "Task: analyse" not in spec2.prompt and "saved" in spec2.prompt  # only what is new
    assert spec2.system_prompt == ""  # the CLI keeps its session's system prompt
    assert second.content == "All done."
    # claude reports cumulative cost per session: the turn costs the difference
    assert second.estimated_cost_usd == pytest.approx(0.015)
    assert backend.remembered == [(owner, "claude", spec1.session_id, "run-1", "seat-1")]
    assert provider.seat_session("run-1", "seat-1").session_id == spec1.session_id
    provider.forget_run("run-1")
    assert provider.seat_session("run-1", "seat-1") is None


async def test_api_key_billing_counts_real_cost():
    transport = FakeTransport([claude_turn("hi", str(uuid.uuid4()), 0.02)])
    provider = CLIProvider("claude", uuid.UUID(USER), FakeBackend(transport, key="sk-ant-api03-key"))
    resp = await provider.complete(request([Message.user("hi")]))
    assert resp.billing == "api_key" and resp.usage.cost_usd == pytest.approx(0.02)
    assert transport.calls[0].persist is False  # no seat: one-shot
    assert transport.calls[0].api_key.get_secret_value() == "sk-ant-api03-key"


async def test_cli_failure_raises_fallback_worthy_error():
    transport = FakeTransport([[*events("codex_usage_limit.jsonl"), {"type": "argus.exit", "ok": False}]])
    provider = CLIProvider("codex", uuid.UUID(USER), FakeBackend(transport))
    with pytest.raises(CLIError) as err:
        await provider.complete(request([Message.user("hi")], run_id="r", member_id="m"))
    assert err.value.category == "quota" and err.value.fallback and not err.value.retryable


async def test_pinned_session_must_belong_to_the_user():
    other = str(uuid.uuid4())
    transport = FakeTransport([])
    provider = CLIProvider("claude", uuid.UUID(USER), FakeBackend(transport))
    with pytest.raises(CLIError) as err:
        await provider.complete(request([Message.user("hi")], run_id="r", member_id="m", cli_session=other))
    assert err.value.category == "session" and not err.value.fallback
    assert transport.calls == []  # never sent to the runner


async def test_pinned_session_is_resumed_when_owned():
    owner = uuid.UUID(USER)
    sid = str(uuid.uuid4())
    transport = FakeTransport([claude_turn("back again", sid, 3.0)])
    transport.sessions.add((USER, "claude", sid))
    backend = FakeBackend(transport)
    backend.owned.add((owner, "claude", sid))
    provider = CLIProvider("claude", owner, backend)
    resp = await provider.complete(
        request([Message.user("continue")], run_id="r", member_id="m", cli_session=sid)
    )
    assert transport.calls[0].resume is True and transport.calls[0].session_id == sid
    # the session's earlier spend is not charged to this turn: fall back to the token estimate
    assert resp.estimated_cost_usd < 3.0


async def test_subscription_disabled_requires_key():
    backend = FakeBackend(FakeTransport([]))
    backend.allow_subscription = False
    provider = CLIProvider("gemini", uuid.UUID(USER), backend)
    with pytest.raises(CLIError) as err:
        await provider.complete(request([Message.user("hi")]))
    assert err.value.category == "auth"


async def test_streamed_text_hides_tool_call_markup():
    sid = str(uuid.uuid4())
    turn = [
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Saving <argus_"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": 'tool_call>{"name": "notes_write", "arguments": {}}</argus_tool_call>',
                },
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": sid,
            "result": 'Saving <argus_tool_call>{"name": "notes_write", "arguments": {}}</argus_tool_call>',
            "usage": {},
        },
        OK_EXIT,
    ]
    provider = CLIProvider("claude", uuid.UUID(USER), FakeBackend(FakeTransport([turn])))
    shown = [e.text async for e in provider.stream(request([Message.user("go")])) if e.type == "text"]
    assert "".join(shown) == "Saving "
