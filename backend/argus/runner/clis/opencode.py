"""opencode (``opencode``, npm ``opencode-ai`` 1.19.5, the 1.x line; OpenCode 2 is a separate beta).

``opencode --pure run --format json --agent argus [-m provider/model] [--variant E] [-s ses_...]``
reads the prompt from stdin (stdin is not a TTY) and prints one JSON object per line (``text``,
``reasoning``, ``tool_use``, ``step_start``, ``step_finish``, ``error``), each with the
``sessionID`` opencode assigned. The caller cannot choose the id: a new session's id is read back
from the first event, and ``-s`` continues it. There is no one-shot mode (sessions always go to the
SQLite store under ``$XDG_DATA_HOME/opencode``).

Tools are allowed by default in 1.x, so every one of them is turned off by configuration the
operator controls:

* the image installs a root-owned managed config at ``/etc/opencode/opencode.json``
  (``backend/docker/runner/policies/opencode.json``). opencode loads it after every other layer
  ("managed settings override everything"). It denies ``*`` and every permission key of 1.19.5 by
  name (the deep merge keeps a key's first position, so ``*`` alone could be shadowed by a more
  specific key from a lower layer), and defines agent ``argus`` with ``permission: "deny"``. A tool
  whose last matching rule is ``* deny`` is removed from the model's tool list;
* ``OPENCODE_PERMISSION={"*":"deny"}`` is merged into ``permission`` after the managed file;
* ``--agent argus`` selects that agent, whose own rules come after the global ones;
* ``run`` auto-rejects any permission request unless ``--auto``/``--yolo``/
  ``--dangerously-skip-permissions``, which are never passed;
* ``--pure`` (no external plugins), ``OPENCODE_DISABLE_PROJECT_CONFIG``,
  ``OPENCODE_DISABLE_CLAUDE_CODE`` (no ``.claude`` prompt or skills),
  ``OPENCODE_DISABLE_EXTERNAL_SKILLS``, ``OPENCODE_DISABLE_LSP_DOWNLOAD``,
  ``OPENCODE_DISABLE_AUTOUPDATE`` and ``OPENCODE_DISABLE_MODELS_FETCH`` (the bundled models.dev
  snapshot is used);
* the runner kills the turn if a ``tool_use`` event shows a tool of its own ran.

``OPENCODE_DISABLE_DEFAULT_PLUGINS`` skips opencode's internal plugins, and in 1.19.5 those include
the OpenAI (ChatGPT sign-in) and GitHub Copilot auth plugins. It is set only when the turn runs on an
API key; a turn on the user's ChatGPT or Copilot sign-in needs them. They add no tools (and any tool
would be denied by the rules above).

No system-prompt flag: the system prompt file is the ``argus`` agent's ``prompt`` as
``{file:<path>}`` in ``OPENCODE_CONFIG_CONTENT`` (opencode substitutes and JSON-escapes the file's
text before parsing). The key goes in the provider's own env var, never argv.
"""

from __future__ import annotations

import json
import re
from typing import Any

from argus.runner.clis import CliPlugin, key_env
from argus.runner.specs import EFFORTS, Binaries, Command, InvokeSpec, LoginSpec, TurnContext, session_ok

# opencode models are "provider/model" (openrouter models carry a second slash). The shared
# MODEL_RE has no "/", so opencode checks its own form here before anything reaches argv.
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,127}$")
# The system prompt path is placed inside "{file:...}" in JSON config text: no "}" or quotes.
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/\-]{1,1024}$")

# Every permission key of 1.19.5: the config schema's keys plus the ones tools and the run
# command ask for (bash, edit/write/patch/apply_patch, plan_enter/plan_exit, ...). Kept in sync
# with backend/docker/runner/policies/opencode.json (a test checks they match).
PERMISSION_KEYS: tuple[str, ...] = (
    "read",
    "edit",
    "write",
    "patch",
    "apply_patch",
    "multiedit",
    "glob",
    "grep",
    "list",
    "bash",
    "shell",
    "task",
    "skill",
    "lsp",
    "todowrite",
    "todoread",
    "question",
    "webfetch",
    "websearch",
    "codesearch",
    "external_directory",
    "doom_loop",
    "plan_enter",
    "plan_exit",
    "workflow_tool_approval",
)

AGENT = "argus"
ENV_FIXED: dict[str, str] = {
    "OPENCODE_PERMISSION": json.dumps({"*": "deny"}),
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
}

# Where opencode keeps its stored sign-ins ($XDG_DATA_HOME/opencode/auth.json), relative to HOME.
AUTH_FILE = ".local/share/opencode/auth.json"


def _xdg(home: str) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_STATE_HOME": f"{home}/.local/state",
    }


def model_arg(model: str | None, key_provider: str | None) -> str | None:
    """The ``-m`` value: ``provider/model``. A bare model takes the key's provider."""
    if not model:
        return None
    provider, sep, rest = model.partition("/")
    if not sep:
        if not key_provider:
            raise ValueError("opencode models are given as provider/model")
        provider, rest = key_provider, model
    if not PROVIDER_RE.match(provider) or not MODEL_ID_RE.match(rest) or ".." in rest or "//" in rest:
        raise ValueError("invalid model name")
    return f"{provider}/{rest}"


def config_content(system_prompt_file: str | None) -> str:
    agent: dict[str, Any] = {"mode": "primary", "permission": "deny"}
    if system_prompt_file:
        if not SAFE_PATH_RE.match(system_prompt_file) or ".." in system_prompt_file:
            raise ValueError("invalid system prompt path")
        agent["prompt"] = "{file:" + system_prompt_file + "}"
    return json.dumps({"agent": {AGENT: agent}, "share": "disabled", "autoupdate": False})


def build(spec: InvokeSpec, binaries: Binaries, turn: TurnContext) -> Command:
    argv = [binaries.opencode, "--pure", "run", "--format", "json", "--agent", AGENT]
    model = model_arg(spec.model, spec.key_provider)
    if model:
        argv += ["-m", model]
    if spec.effort:
        if spec.effort not in EFFORTS:  # a Literal in InvokeSpec; argv never trusts it
            raise ValueError("invalid effort")
        argv += ["--variant", spec.effort]
    if spec.resume:
        if not session_ok("opencode", spec.session_id):
            raise ValueError("invalid session_id")
        assert spec.session_id is not None
        argv += ["-s", spec.session_id]
    # A new session (persisted or not) gets an id from opencode: nothing to pass. There is no
    # no-persist mode, so persist=False only means Argus does not record the id.
    keys: dict[str, str] = {}
    if spec.api_key is not None:
        keys = key_env("opencode", spec.key_provider, spec.api_key.get_secret_value(), spec.model)
        if not keys:
            raise ValueError("unknown key provider for opencode")
        keys["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
    # The fixed switches are applied last: nothing else can override them.
    env = keys | _xdg(turn.home) | ENV_FIXED
    env["OPENCODE_CONFIG_CONTENT"] = config_content(turn.system_prompt_file)
    return Command(argv, env, spec.prompt)


def session_from_event(event: dict[str, Any]) -> str | None:
    sid = event.get("sessionID")
    return sid if isinstance(sid, str) else None


def tools_reported(event: dict[str, Any]) -> list[Any] | None:
    # opencode prints no tool list at start. A tool_use event means one of its own tools ran
    # (completed or failed); "invalid" is opencode's stand-in for a call to a tool that does not
    # exist, which is what a denied (removed) tool looks like, so it is not counted.
    if event.get("type") != "tool_use":
        return None
    part = event.get("part")
    if not isinstance(part, dict):
        part = {}
    tool = part.get("tool")
    if tool == "invalid":
        return None
    return [tool if isinstance(tool, str) and tool else "<unknown tool>"]


# ----------------------------------------------------------------------------------- status
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_COUNT_RE = re.compile(r"\b(\d+)\s+credentials?\b")


def parse_status(code: int | None, output: str) -> dict[str, Any]:
    """``opencode auth list``: provider names and credential types only, never the values."""
    text = _ANSI_RE.sub("", output or "")
    count_match = _COUNT_RE.search(text)
    if code != 0 or count_match is None:
        return {"logged_in": None, "method": None, "verified": False, "detail": "Sign-in state unknown."}
    count = int(count_match.group(1))
    # Only the "Credentials" section (the "Environment" section lists env vars, not sign-ins).
    section = text.split("Environment", 1)[0]
    methods: list[str] = []
    for line in section.splitlines():
        words = line.strip(" \t│●◇◆┌└").split()
        if len(words) < 2:
            continue
        kind, name = words[-1], " ".join(words[:-1]).lower()
        if kind == "oauth" and ("copilot" in name):
            found = "github_copilot"
        elif kind == "oauth" and "openai" in name:
            found = "chatgpt"
        elif kind == "oauth":
            found = "oauth"
        elif kind in ("api", "wellknown"):
            found = "api_key"
        else:
            continue
        if found not in methods:
            methods.append(found)
    return {
        "logged_in": count > 0,
        "method": ", ".join(methods) if count > 0 and methods else None,
        "verified": False,
        "detail": f"{count} stored sign-in{'s' if count != 1 else ''} (not verified until first use)."
        if count
        else "No sign-in found.",
    }


# ---------------------------------------------------------------------------------- sign-in
# ``opencode auth login -p <provider> [-m <method label>]`` runs @clack prompts, so it needs a PTY.
# Both methods are device-code flows: opencode prints "Go to: <url>" and "Enter code: <code>",
# then polls and writes $XDG_DATA_HOME/opencode/auth.json. It exits 0 even when the authorization
# fails ("Failed to authorize" then "Done"), so success is the credentials file being written.
# GitHub Copilot asks "Select GitHub deployment type" (GitHub.com / GitHub Enterprise) first.
LOGIN = LoginSpec(
    cli="opencode",
    args=(),
    flow="device_code",
    url_hosts=("github.com", "auth.openai.com"),
    method_args={
        "github_copilot": ("auth", "login", "-p", "github-copilot"),
        "chatgpt": ("auth", "login", "-p", "openai", "-m", "ChatGPT Pro/Plus (headless)"),
    },
    pty=True,
    env={
        "TERM": "xterm-256color",
        "OPENCODE_PURE": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    },
    success_file=AUTH_FILE,
    # GitHub Copilot first asks "Select GitHub deployment type"; Enter keeps GitHub.com.
    answers=((re.compile(r"Select GitHub deployment type"), "\r"),),
)


PLUGIN = CliPlugin(
    name="opencode",
    label="opencode",
    build=build,
    suggested_models=(
        "default",
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.5",
        "google/gemini-2.5-pro",
        "github-copilot/claude-sonnet-5",
        "openrouter/anthropic/claude-sonnet-5",
        "mistral/mistral-large-latest",
    ),
    key_providers={
        "anthropic": {"ANTHROPIC_API_KEY": "{key}"},
        "openai": {"OPENAI_API_KEY": "{key}"},
        "google": {"GOOGLE_GENERATIVE_AI_API_KEY": "{key}"},
        "openrouter": {"OPENROUTER_API_KEY": "{key}"},
        "mistral": {"MISTRAL_API_KEY": "{key}"},
    },
    supports_effort=True,
    login=LOGIN,
    # `auth logout <provider>` needs the provider (and prompts without one): sign-out removes the
    # whole credentials file instead, which signs out of every provider.
    logout=(None, (AUTH_FILE,)),
    status_args=("auth", "list"),
    parse_status=parse_status,
    session_from_event=session_from_event,
    tools_reported=tools_reported,
)
