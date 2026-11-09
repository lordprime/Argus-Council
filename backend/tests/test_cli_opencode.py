"""opencode (``opencode-ai`` 1.19.5) as a council seat: argv, env, the operator's managed config,
the ``run --format json`` parser, the tool-use check, sign-in in a PTY, status and sign-out, and a
full turn through the runner against a fake ``opencode`` executable that prints what the pinned
version prints.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from argus.providers.cli.parsers import make_parser
from argus.runner.clis import key_env, login_spec_for, logout_spec_for, plugin
from argus.runner.clis.opencode import PERMISSION_KEYS, config_content, model_arg
from argus.runner.login import LoginManager
from argus.runner.process import Runner, RunnerConfig, _base_env, _child_setup, user_home
from argus.runner.specs import Binaries, InvokeSpec, TurnContext, build_command, session_ok

USER_A = "11111111-1111-4111-8111-111111111111"
POLICY = Path(__file__).resolve().parents[1] / "docker" / "runner" / "policies" / "opencode.json"
SID = "ses_4f2a7c1e9b4dXy81Qw"
FORBIDDEN = ("--auto", "--yolo", "--dangerously-skip-permissions", "--share", "--attach", "--thinking")
PROMPT_FILE = "/h/.argus/prompts/p.md"


def turn(tmp_path: Path, system_prompt_file: str | None = PROMPT_FILE) -> TurnContext:
    return TurnContext(
        home=str(tmp_path),
        cwd=str(tmp_path / "w"),
        turn_dir=str(tmp_path / "t"),
        system_prompt_file=system_prompt_file,
    )


def spec(**kw) -> InvokeSpec:
    return InvokeSpec(user_id=USER_A, cli="opencode", prompt="hello", **kw)


def build(tmp_path: Path, s: InvokeSpec | None = None, system_prompt_file: str | None = PROMPT_FILE, **kw):
    s = s or spec(**kw)
    return build_command(
        s, binaries=Binaries(), system_prompt_file=None, turn=turn(tmp_path, system_prompt_file)
    )


def assert_safe(argv: list[str]) -> None:
    assert argv[:7] == ["opencode", "--pure", "run", "--format", "json", "--agent", "argus"]
    for bad in FORBIDDEN:
        assert bad not in argv
    assert "hello" not in argv


# ------------------------------------------------------------------------------------------- argv
def test_new_session_lets_opencode_assign_the_id(tmp_path):
    cmd = build(tmp_path)
    assert_safe(cmd.argv)
    assert cmd.argv == ["opencode", "--pure", "run", "--format", "json", "--agent", "argus"]
    assert cmd.stdin == "hello"
    assert cmd.files == {} and cmd.result_file is None and cmd.protocol == "stdio"


def test_resume_continues_the_session(tmp_path):
    cmd = build(tmp_path, session_id=SID, resume=True)
    assert_safe(cmd.argv)
    assert cmd.argv[-2:] == ["-s", SID]


def test_one_shot_has_no_flag_and_no_session(tmp_path):
    cmd = build(tmp_path, persist=False)
    assert_safe(cmd.argv)
    assert "-s" not in cmd.argv and "--continue" not in cmd.argv and "-c" not in cmd.argv


def test_session_ids_are_checked():
    assert session_ok("opencode", SID)
    assert not session_ok("opencode", "3f2a7c1e-9b4d-4e8f-a1c2-5d6e7f8a9b0c")
    assert not session_ok("opencode", "ses_short")
    assert not session_ok("opencode", "ses_abc/../../etc")
    with pytest.raises(ValueError):
        spec(session_id=SID)  # opencode assigns ids: a session id only to resume
    with pytest.raises(ValueError):
        spec(session_id="3f2a7c1e-9b4d-4e8f-a1c2-5d6e7f8a9b0c", resume=True)


def test_model_and_effort(tmp_path):
    cmd = build(
        tmp_path, model="claude-sonnet-5", effort="high", api_key="sk-ant-x", key_provider="anthropic"
    )
    assert_safe(cmd.argv)
    assert cmd.argv[7:] == ["-m", "anthropic/claude-sonnet-5", "--variant", "high"]
    # provider/model as opencode expects (the shared MODEL_RE has no "/", so no InvokeSpec yet)
    s = spec().model_copy(update={"model": "openrouter/anthropic/claude-sonnet-5"})
    assert build(tmp_path, s).argv[7:] == ["-m", "openrouter/anthropic/claude-sonnet-5"]


@pytest.mark.parametrize(
    "model",
    [
        "-rf/x",
        "anthropic/-m",
        "Anthropic/x",
        "anthropic/../x",
        "anthropic//x",
        "a b/x",
        "x/y z",
        "x/y;rm",
        "x/{file:y}",
    ],
)
def test_bad_models_never_reach_argv(model):
    with pytest.raises(ValueError):
        model_arg(model, None)


def test_bare_model_needs_a_provider(tmp_path):
    with pytest.raises(ValueError):
        build(tmp_path, model="gpt-5.5")  # no key provider to take it from
    assert model_arg(None, "openai") is None
    assert model_arg("gpt-5.5", "openai") == "openai/gpt-5.5"


def test_env_turns_tools_off_and_keeps_opencode_in_the_users_home(tmp_path):
    cmd = build(tmp_path)
    env = cmd.env
    assert json.loads(env["OPENCODE_PERMISSION"]) == {"*": "deny"}
    for name in (
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_MODELS_FETCH",
    ):
        assert env[name] == "1"
    assert env["XDG_CONFIG_HOME"] == f"{tmp_path}/.config"
    assert env["XDG_DATA_HOME"] == f"{tmp_path}/.local/share"
    assert env["XDG_CACHE_HOME"] == f"{tmp_path}/.cache"
    assert env["XDG_STATE_HOME"] == f"{tmp_path}/.local/state"
    # Signed-in turns keep the internal plugins: they carry the ChatGPT and Copilot auth.
    assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" not in env
    assert not any(k.endswith("_API_KEY") for k in env)


def test_system_prompt_is_the_argus_agents_prompt_file(tmp_path):
    content = json.loads(build(tmp_path).env["OPENCODE_CONFIG_CONTENT"])
    assert content["agent"]["argus"] == {
        "mode": "primary",
        "permission": "deny",
        "prompt": "{file:" + PROMPT_FILE + "}",
    }
    assert content["share"] == "disabled"
    bare = json.loads(build(tmp_path, system_prompt_file=None).env["OPENCODE_CONFIG_CONTENT"])
    assert bare["agent"]["argus"] == {"mode": "primary", "permission": "deny"}
    for bad in ("/h/p}.md", '/h/"x.md', "relative.md", "/h/../etc/passwd"):
        with pytest.raises(ValueError):
            config_content(bad)


@pytest.mark.parametrize(
    ("provider", "var"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("google", "GOOGLE_GENERATIVE_AI_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("mistral", "MISTRAL_API_KEY"),
    ],
)
def test_key_env_per_provider(tmp_path, provider, var):
    assert key_env("opencode", provider, "sk-1", None) == {var: "sk-1"}
    cmd = build(tmp_path, api_key="sk-secret", key_provider=provider)
    assert cmd.env[var] == "sk-secret"
    assert cmd.env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert "sk-secret" not in " ".join(cmd.argv)


def test_unknown_key_provider_is_refused(tmp_path):
    assert key_env("opencode", "groq", "k", None) == {}
    with pytest.raises(ValueError):
        build(tmp_path, api_key="k", key_provider="groq")
    with pytest.raises(ValueError):
        build(tmp_path, api_key="k")


def test_plugin_metadata():
    p = plugin("opencode")
    assert p.label == "opencode" and p.supports_effort
    assert set(p.key_providers) == {"anthropic", "openai", "google", "openrouter", "mistral"}
    assert "default" in p.suggested_models
    assert all(m == "default" or model_arg(m, None) == m for m in p.suggested_models)


# ------------------------------------------------------------------------------------ the policy
def test_managed_config_denies_everything():
    policy = json.loads(POLICY.read_text())
    perm = policy["permission"]
    assert next(iter(perm)) == "*" and perm["*"] == "deny"
    assert set(perm) == {"*", *PERMISSION_KEYS}
    assert set(perm.values()) == {"deny"}
    for key in (
        "read",
        "edit",
        "glob",
        "grep",
        "list",
        "bash",
        "task",
        "skill",
        "lsp",
        "todowrite",
        "question",
        "webfetch",
        "websearch",
        "external_directory",
        "doom_loop",
    ):
        assert perm[key] == "deny"
    assert policy["agent"]["argus"]["mode"] == "primary"
    assert policy["agent"]["argus"]["permission"] == "deny"
    assert "prompt" not in policy["agent"]["argus"]  # the per-turn config content brings it
    assert policy["share"] == "disabled" and policy["autoupdate"] is False
    text = POLICY.read_text()
    assert "{env:" not in text and "{file:" not in text  # opencode substitutes these


# ---------------------------------------------------------------------------------------- parser
def ev(kind: str, **kw) -> dict:
    return {"type": kind, "timestamp": 1762770000000, "sessionID": SID, **kw}


def text_part(pid: str, text: str) -> dict:
    return {
        "id": pid,
        "sessionID": SID,
        "messageID": "msg_1",
        "type": "text",
        "text": text,
        "time": {"start": 1, "end": 2},
    }


def step_finish(cost: float, inp: int, out: int, reasoning: int = 0, read: int = 0, write: int = 0) -> dict:
    tokens = {
        "total": inp + out,
        "input": inp,
        "output": out,
        "reasoning": reasoning,
        "cache": {"read": read, "write": write},
    }
    return ev(
        "step_finish",
        part={"id": "prt_f", "type": "step-finish", "reason": "stop", "cost": cost, "tokens": tokens},
    )


def test_parser_text_usage_cost_and_session():
    p = make_parser("opencode")
    out = []
    for e in (
        ev("step_start", part={"id": "prt_s", "type": "step-start"}),
        ev("text", part=text_part("prt_1", "The council")),
        ev("text", part=text_part("prt_1", "The council")),  # a repeat of the same part
        step_finish(0.0125, 1000, 200, reasoning=50, read=300, write=40),
        ev("text", part=text_part("prt_2", "agrees.")),
        step_finish(0.0025, 100, 20),
    ):
        out += p.feed(e)
    assert [(s.type, s.text) for s in out] == [("text", "The council"), ("text", "\n\nagrees.")]
    res = p.finish({"type": "argus.exit", "ok": True, "session_id": SID})
    assert res.text == "The council\n\nagrees."
    assert res.session_id == SID
    assert res.usage.input_tokens == 1100 and res.usage.output_tokens == 270
    assert res.usage.cache_read_tokens == 300 and res.usage.cache_write_tokens == 40
    assert res.reported_cost_usd == pytest.approx(0.015)
    assert res.failure is None


def test_parser_reasoning_is_thinking_and_ignores_tool_and_synthetic_parts():
    p = make_parser("opencode")
    reasoning = {"id": "prt_r", "type": "reasoning", "text": "Weighing it.", "time": {"start": 1, "end": 2}}
    out = p.feed(ev("reasoning", part=reasoning))
    assert [(s.type, s.text) for s in out] == [("thinking", "Weighing it.")]
    assert p.feed(ev("text", part=text_part("prt_x", "x") | {"synthetic": True})) == []
    assert p.feed(ev("tool_use", part={"type": "tool", "tool": "bash"})) == []
    res = p.finish({"type": "argus.exit", "ok": True})
    assert res.thinking == "Weighing it." and res.text == ""
    assert res.reported_cost_usd is None


def api_error(status: int | None, message: str, body: str = "") -> dict:
    data = {"message": message, "isRetryable": False, "responseBody": body}
    if status is not None:
        data["statusCode"] = status
    return ev("error", error={"name": "APIError", "data": data})


@pytest.mark.parametrize(
    ("event", "category"),
    [
        (api_error(429, "Too Many Requests"), "rate_limit"),
        (api_error(429, "Rate limit reached for requests"), "rate_limit"),
        (
            api_error(429, "You exceeded your current quota", '{"error":{"code":"insufficient_quota"}}'),
            "quota",
        ),
        (api_error(429, "Usage limit reached. It will reset in 3 hours."), "quota"),
        (
            api_error(402, "Payment required", '{"type":"error","error":{"type":"GoUsageLimitError"}}'),
            "quota",
        ),
        (api_error(401, "invalid x-api-key"), "auth"),
        (api_error(503, "Service Unavailable"), "unavailable"),
        (api_error(404, "model: claude-nope not found"), "invalid"),
        (
            ev(
                "error",
                error={
                    "name": "ProviderAuthError",
                    "data": {"providerID": "openai", "message": "token expired"},
                },
            ),
            "auth",
        ),
        (
            ev("error", error={"name": "ContextOverflowError", "data": {"message": "prompt is too long"}}),
            "invalid",
        ),
        (ev("error", error={"name": "UnknownError", "data": {"message": "Usage limit reached"}}), "quota"),
        (
            ev(
                "error",
                error={"name": "UnknownError", "data": {"message": "socket hang up: connection reset"}},
            ),
            "unavailable",
        ),
    ],
)
def test_parser_error_categories(event, category):
    p = make_parser("opencode")
    p.feed(event)
    res = p.finish({"type": "argus.exit", "ok": False, "exit_code": 1, "error": "exit_status", "detail": ""})
    assert res.failure is not None and res.failure.category == category


def test_parser_session_not_found_and_runner_failures():
    p = make_parser("opencode")
    res = p.finish(
        {"type": "argus.exit", "ok": False, "error": "exit_status", "detail": "Error: Session not found"}
    )
    assert res.failure is not None and res.failure.category == "session"
    res = make_parser("opencode").finish(None)
    assert res.failure is not None and res.failure.category == "unavailable"


# ------------------------------------------------------------------------------------ runner hooks
def test_session_from_event_and_tools_reported():
    p = plugin("opencode")
    assert p.session_from_event(ev("step_start", part={})) == SID
    assert p.session_from_event({"type": "text"}) is None
    assert p.session_from_event({"type": "text", "sessionID": 7}) is None
    assert p.tools_reported(ev("text", part=text_part("prt_1", "x"))) is None
    assert p.tools_reported(ev("step_finish", part={})) is None
    tool = {"type": "tool", "tool": "bash", "state": {"status": "completed"}}
    assert p.tools_reported(ev("tool_use", part=tool)) == ["bash"]
    assert p.tools_reported(ev("tool_use", part={"type": "tool", "state": {"status": "error"}})) == [
        "<unknown tool>"
    ]
    # "invalid" is opencode's stand-in for a call to a tool that is not there (it was denied)
    assert p.tools_reported(ev("tool_use", part={"type": "tool", "tool": "invalid"})) is None


# --------------------------------------------------------------------------------- status / logout
AUTH_LIST = (
    "\x1b[0m\n┌  Credentials \x1b[2m~/.local/share/opencode/auth.json\n│\n"
    "●  GitHub Copilot \x1b[2moauth\n│\n●  OpenAI \x1b[2moauth\n│\n●  Anthropic \x1b[2mapi\n│\n"
    "└  3 credentials\n\n┌  Environment\n│\n●  Mistral \x1b[2mMISTRAL_API_KEY\n│\n└  1 environment variable\n"
)


def test_status_parse_never_returns_values():
    parse = plugin("opencode").parse_status
    assert parse is not None
    st = parse(0, AUTH_LIST)
    assert st["logged_in"] is True and st["verified"] is False
    assert st["method"] == "github_copilot, chatgpt, api_key"
    assert "3 stored" in st["detail"] and "auth.json" not in json.dumps(st)
    empty = parse(0, "┌  Credentials ~/.local/share/opencode/auth.json\n│\n└  0 credentials\n")
    assert empty == {"logged_in": False, "method": None, "verified": False, "detail": "No sign-in found."}
    assert parse(1, "Error: something")["logged_in"] is None
    assert parse(None, "timed out")["logged_in"] is None
    assert plugin("opencode").status_args == ("auth", "list")


def test_logout_removes_the_credentials_file():
    assert logout_spec_for("opencode") == (None, (".local/share/opencode/auth.json",))


# --------------------------------------------------------------------------- through the runner
FAKE_OPENCODE = r"""
import json, os, sys
argv = sys.argv[1:]
home = os.environ["HOME"]
auth = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local/share")), "opencode", "auth.json")
if argv == ["auth", "list"]:
    n = len(json.load(open(auth))) if os.path.exists(auth) else 0
    print("┌  Credentials ~/.local/share/opencode/auth.json")
    if n:
        print("│\n●  OpenAI \x1b[2moauth")
    print("│\n└  %d credentials" % n)
    sys.exit(0)
for bad in ("--auto", "--yolo", "--dangerously-skip-permissions"):
    assert bad not in argv, bad
assert argv[:6] == ["--pure", "run", "--format", "json", "--agent", "argus"], argv
assert json.loads(os.environ["OPENCODE_PERMISSION"]) == {"*": "deny"}
cfg = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
agent = cfg["agent"]["argus"]
assert agent["permission"] == "deny"
system = ""
if "prompt" in agent:
    path = agent["prompt"][len("{file:"):-1]
    system = open(path).read()
prompt = sys.stdin.read()
sid = argv[argv.index("-s") + 1] if "-s" in argv else "ses_" + "N3wSess10nAbc"
def out(kind, **kw):
    print(json.dumps({"type": kind, "timestamp": 1, "sessionID": sid, **kw}), flush=True)
out("step_start", part={"id": "prt_s", "type": "step-start"})
if "ratelimit" in prompt:
    out("error", error={"name": "APIError", "data": {"message": "Too Many Requests", "statusCode": 429, "isRetryable": True}})
    print("Error: Too Many Requests", file=sys.stderr)
    sys.exit(1)
if "tool" in prompt:
    out("tool_use", part={"id": "prt_t", "type": "tool", "tool": "bash", "state": {"status": "completed"}})
reply = "system=%s; prompt=%s; resumed=%s; key=%s" % (system, prompt, "-s" in argv, bool(os.environ.get("ANTHROPIC_API_KEY")))
out("text", part={"id": "prt_1", "type": "text", "text": reply, "time": {"start": 1, "end": 2}})
out("step_finish", part={"id": "prt_f", "type": "step-finish", "reason": "stop", "cost": 0.001,
    "tokens": {"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}}})
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
        home_root=tmp_path / "users", binaries=Binaries(opencode=_exe(bins / "opencode", FAKE_OPENCODE))
    )
    return Runner(cfg)


async def collect(runner: Runner, **kw) -> list[dict]:
    return [e async for e in runner.invoke(InvokeSpec(user_id=USER_A, cli="opencode", **kw))]


def parse_all(evs: list[dict]):
    parser = make_parser("opencode")
    for e in evs[:-1]:
        parser.feed(e)
    return parser.finish(evs[-1])


async def test_turn_creates_then_resumes_the_session(runner):
    evs = await collect(
        runner,
        prompt="hi",
        system_prompt="You are a council seat.",
        model="claude-sonnet-5",
        api_key="sk-ant-supersecret",
        key_provider="anthropic",
    )
    assert evs[-1]["type"] == "argus.exit" and evs[-1]["ok"], evs[-1]
    sid = evs[-1]["session_id"]
    assert sid == "ses_N3wSess10nAbc"
    res = parse_all(evs)
    assert res.text == "system=You are a council seat.; prompt=hi; resumed=False; key=True"
    assert res.session_id == sid and res.reported_cost_usd == pytest.approx(0.001)
    assert runner.has_session(USER_A, "opencode", sid)

    evs = await collect(runner, prompt="again", session_id=sid, resume=True)
    assert evs[-1]["ok"] and evs[-1]["session_id"] == sid
    assert parse_all(evs).text == "system=; prompt=again; resumed=True; key=False"


async def test_turn_rate_limited(runner):
    evs = await collect(runner, prompt="ratelimit please")
    assert not evs[-1]["ok"]
    res = parse_all(evs)
    assert res.failure is not None and res.failure.category == "rate_limit"


async def test_turn_with_a_tool_of_its_own_is_killed(runner):
    evs = await collect(runner, prompt="use a tool")
    assert evs[-1]["error"] == "tools_not_disabled"
    assert not any(e.get("type") == "tool_use" for e in evs)


async def test_status_and_logout_through_the_runner(runner):
    st = await runner.status(USER_A, "opencode")
    assert st["logged_in"] is False
    auth = user_home(runner.cfg, USER_A) / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "r", "access": "a", "expires": 0}}))
    st = await runner.status(USER_A, "opencode")
    assert st["logged_in"] is True and st["method"] == "chatgpt"
    assert "refresh" not in json.dumps(st)
    out = await runner.logout(USER_A, "opencode")
    assert not auth.exists()
    assert out["status"]["logged_in"] is False


# ------------------------------------------------------------------------------------- sign-in
# What `opencode auth login` prints in a terminal (@clack/prompts): an intro, then for a device
# flow "Go to: <url>" and "Enter code: <code>" as log lines, a spinner while it polls, and
# "Login successful" / "Done". It writes $XDG_DATA_HOME/opencode/auth.json and exits 0, also when
# the authorization fails ("Failed to authorize", "Done").
FAKE_LOGIN = r"""
import json, os, sys, time
argv = sys.argv[1:]
assert sys.stdin.isatty(), "clack needs a terminal"
assert os.environ.get("OPENCODE_PURE") == "1"
assert "OPENCODE_DISABLE_DEFAULT_PLUGINS" not in os.environ  # the auth plugins must load
home = os.environ["HOME"]
print("\x1b[?25l┌  Add credential\n│")
if argv[:4] == ["auth", "login", "-p", "github-copilot"]:
    print("◆  Select GitHub deployment type\n│  ● GitHub.com (Public)\n│  ○ GitHub Enterprise\n└", flush=True)
    sys.stdin.readline()  # clack waits for a key press
    url, code = "https://github.com/login/device", "ABCD-1234"
elif argv == ["auth", "login", "-p", "openai", "-m", "ChatGPT Pro/Plus (headless)"]:
    url, code = "https://auth.openai.com/codex/device", "QX7P-R2M4K"
else:
    print("Unknown method", argv); sys.exit(1)
print("●  Go to: \x1b[36m%s\x1b[39m\n│\n●  Enter code: %s\n│" % (url, code))
print("◒  Waiting for authorization...", flush=True)
time.sleep(float(os.environ.get("FAKE_WAIT", "0.6")))
if os.environ.get("FAKE_FAIL"):
    print("■  Failed to authorize\n│\n└  Done"); sys.exit(0)
path = os.path.join(home, ".local", "share", "opencode", "auth.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "w").write(json.dumps({"openai": {"type": "oauth", "refresh": "r", "access": "a", "expires": 0}}))
print("◇  Login successful\n│\n└  Done")
time.sleep(30)
"""


def _logins(tmp_path: Path, deadline_s: float = 20, **env: str) -> LoginManager:
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    exe = _exe(bins / "opencode", FAKE_LOGIN)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return LoginManager(
        Binaries(opencode=exe),
        home_for=lambda user_id: home,
        base_env=lambda h: _base_env(h) | env,
        child_setup=_child_setup,
        deadline_s=deadline_s,
    )


async def wait_state(m: LoginManager, *states: str, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = m.state(USER_A, "opencode")
        if st["state"] in states:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"state stayed {m.state(USER_A, 'opencode')}")


def test_login_spec():
    s = login_spec_for("opencode")
    assert s is not None and s.flow == "device_code" and s.pty
    assert s.methods == ("github_copilot", "chatgpt")
    assert s.argv(Binaries(), "chatgpt") == [
        "opencode",
        "auth",
        "login",
        "-p",
        "openai",
        "-m",
        "ChatGPT Pro/Plus (headless)",
    ]
    assert s.argv(Binaries(), "github_copilot") == ["opencode", "auth", "login", "-p", "github-copilot"]
    assert s.url_hosts == ("github.com", "auth.openai.com")
    assert s.success_file == ".local/share/opencode/auth.json"
    assert s.reset_files == ()  # other providers' sign-ins stay
    assert "claude" not in json.dumps(s.method_args).lower() and "anthropic" not in json.dumps(s.method_args)


async def test_chatgpt_headless_device_code(tmp_path):
    m = _logins(tmp_path)
    try:
        await m.start(USER_A, "opencode", "chatgpt")
        st = await wait_state(m, "waiting", "succeeded")
        if st["state"] == "waiting":
            assert st["verification_url"] == "https://auth.openai.com/codex/device"
            assert st["user_code"] == "QX7P-R2M4K"
        st = await wait_state(m, "succeeded")
        assert st["error"] is None
    finally:
        await m.close()


async def test_failed_authorization_exits_zero_but_is_not_success(tmp_path):
    m = _logins(tmp_path, FAKE_FAIL="1")
    try:
        await m.start(USER_A, "opencode", "chatgpt")
        st = await wait_state(m, "failed")
        assert st["error"] == "exited"
    finally:
        await m.close()


async def test_copilot_deployment_prompt_is_answered_with_the_default(tmp_path):
    # Copilot asks "Select GitHub deployment type" before it shows the device code; the login spec
    # presses Enter once (GitHub.com), then the link and code are relayed as usual.
    m = _logins(tmp_path)
    try:
        await m.start(USER_A, "opencode", "github_copilot")
        st = await wait_state(m, "waiting")
        assert st["verification_url"] == "https://github.com/login/device" and st["user_code"] == "ABCD-1234"
        st = await wait_state(m, "succeeded")
    finally:
        await m.close()
