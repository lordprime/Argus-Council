"""What the runner may launch, and exactly how.

The API never sends an argv: it sends a validated :class:`InvokeSpec` and the runner builds the
command line here from a fixed template per CLI. Every value that reaches argv is checked
against a strict pattern, the command is always an argument list (never a shell), and the
prompt and system prompt travel through stdin and a file, not argv.

Every vendor CLI ships its own tools (shell, file edit, web fetch, MCP). Argus's tools,
guardrails and approvals must stay authoritative, so each template turns all of them off:

* claude (Claude Code 2.1.x, ``claude -p``): ``--tools ""`` plus ``--disallowedTools "*"``,
  ``--permission-mode dontAsk``, ``--permission-prompts none``, ``--strict-mcp-config`` without an
  ``--mcp-config`` (no MCP servers) and ``--disable-slash-commands``. With an API key it also runs
  ``--bare`` (no hooks, plugins, CLAUDE.md or OAuth/keychain reads).
* codex (``codex exec --json``): ``--sandbox read-only``, ``approval_policy=never`` (never the
  ``--dangerously-bypass-*`` flags), web search disabled, the shell, exec, plugin, browser,
  computer-use and multi-agent features disabled, ``--ignore-user-config`` and ``--ignore-rules``.
* gemini (``gemini --output-format stream-json``): never ``--yolo``; ``--approval-mode default``
  (prompts are denied when headless) and the image installs an admin-tier policy
  (``/etc/gemini-cli/policies``) that denies every tool, which user settings cannot override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

CliName = Literal["claude", "codex", "gemini", "copilot", "kiro", "qwen", "opencode", "goose", "vibe"]
CLI_NAMES: tuple[CliName, ...] = (
    "claude",
    "codex",
    "gemini",
    "copilot",
    "kiro",
    "qwen",
    "opencode",
    "goose",
    "vibe",
)
# CLIs whose template lives in their own module, argus/runner/clis/<name>.py (see that package).
PLUGIN_CLIS: tuple[str, ...] = ("copilot", "kiro", "qwen", "opencode", "goose", "vibe")
# Session ids per CLI: UUIDs, except opencode which assigns its own "ses_..." ids.
SESSION_PATTERNS: dict[str, re.Pattern[str]] = {"opencode": re.compile(r"^ses_[A-Za-z0-9]{8,60}$")}
# CLIs that assign their own session id on a new session (read back from the output).
CLI_ASSIGNED_SESSIONS: frozenset[str] = frozenset({"codex", "kiro", "opencode", "vibe"})
KEY_PROVIDER_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def session_ok(cli: str, session_id: str | None) -> bool:
    if not session_id:
        return False
    return bool(SESSION_PATTERNS.get(cli, UUID_RE).match(session_id))


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Model ids and aliases: "sonnet", "claude-opus-5-5[1m]", "gpt-5.5-codex", "gemini-2.5-pro", and
# provider-qualified ids of harness CLIs ("openrouter/anthropic/claude-sonnet-4.5").
# No leading dash (could be read as a flag), no whitespace, no shell metacharacters, no "..".
MODEL_RE = re.compile(r"^(?!.*\.\.)(?!.*//)[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,99}(?<!/)$")
RUN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,99}$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

MAX_PROMPT_CHARS = 2_000_000  # below every CLI's stdin cap (claude 10 MB, gemini 8 MB)
MAX_SYSTEM_PROMPT_CHARS = 200_000

# Codex features that would give the model tools of its own (verified with
# `codex features list` on codex-cli 0.163.0). Unknown names make codex exit, so keep in sync
# with the pinned version in deploy/runner/package.json.
CODEX_DISABLED_FEATURES: tuple[str, ...] = (
    "shell_tool",
    "unified_exec",
    "multi_agent",
    "plugins",
    "apps",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "computer_use",
    "hooks",
    "view_image",
    "sleep_tool",
    "tool_suggest",
)

# Environment variable each CLI reads its API key from (documented by each vendor). The key is set
# only on that user's process; anything else from the runner's environment is not inherited.
API_KEY_ENV: dict[str, str] = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "CODEX_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class InvokeSpec(BaseModel):
    """One CLI turn, as the API asks for it. Validated again by the runner (defence in depth)."""

    user_id: str
    cli: CliName
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    system_prompt: str = Field(default="", max_length=MAX_SYSTEM_PROMPT_CHARS)
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # claude/gemini: the id to create (resume=False) or continue (resume=True).
    # codex: only used with resume=True (codex assigns ids itself; we read thread.started).
    session_id: str | None = None
    resume: bool = False
    persist: bool = True  # False: one-shot, nothing written to the CLI's session store
    run_key: str | None = None  # names the per-run working directory
    api_key: SecretStr | None = None
    # Harness CLIs (qwen, opencode, goose): which model provider the API key belongs to.
    key_provider: str | None = None
    timeout_s: int = Field(default=600, ge=5, le=3600)

    @field_validator("user_id")
    @classmethod
    def _uuid_user(cls, v: str) -> str:
        if not UUID_RE.match(v):
            raise ValueError("user_id must be a UUID")
        return v.lower()

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or v in ("", "default"):
            return None
        if not MODEL_RE.match(v):
            raise ValueError("invalid model name")
        return v

    @field_validator("session_id")
    @classmethod
    def _session(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not (UUID_RE.match(v) or any(p.match(v) for p in SESSION_PATTERNS.values())):
            raise ValueError("invalid session_id")
        return v if v.startswith("ses_") else v.lower()

    @field_validator("key_provider")
    @classmethod
    def _key_provider(cls, v: str | None) -> str | None:
        if v is not None and not KEY_PROVIDER_RE.match(v):
            raise ValueError("invalid key_provider")
        return v

    @field_validator("run_key")
    @classmethod
    def _run_key(cls, v: str | None) -> str | None:
        if v is not None and not RUN_KEY_RE.match(v):
            raise ValueError("invalid run_key")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> InvokeSpec:
        if self.resume and not self.session_id:
            raise ValueError("resume needs a session_id")
        if self.resume and not self.persist:
            raise ValueError("a resumed session must persist")
        if self.session_id and not session_ok(self.cli, self.session_id):
            raise ValueError("invalid session_id for this CLI")
        if self.cli in CLI_ASSIGNED_SESSIONS and self.session_id and not self.resume:
            raise ValueError(f"{self.cli} assigns its own session ids; pass session_id only to resume")
        return self


@dataclass
class Command:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: str = ""
    # Files to write before the turn (absolute paths inside the user's HOME), removed afterwards.
    files: dict[str, str] = field(default_factory=dict)
    # A JSON file the CLI writes during the turn; the runner emits it as one argus.result_file event.
    result_file: str | None = None
    # "stdio": the CLI prints JSON lines; "acp": the runner drives the Agent Client Protocol.
    protocol: Literal["stdio", "acp"] = "stdio"


@dataclass(frozen=True)
class TurnContext:
    """Where one turn runs. ``turn_dir`` is private to the turn and removed after it."""

    home: str
    cwd: str
    turn_dir: str
    system_prompt_file: str | None  # holds spec.system_prompt when it is not empty


@dataclass(frozen=True)
class Binaries:
    claude: str = "claude"
    codex: str = "codex"
    gemini: str = "gemini"
    copilot: str = "copilot"
    kiro: str = "kiro-cli"
    qwen: str = "qwen"
    opencode: str = "opencode"
    goose: str = "goose"
    vibe: str = "vibe"

    def for_cli(self, cli: str) -> str:
        return getattr(self, cli)


def build_command(
    spec: InvokeSpec, *, binaries: Binaries, system_prompt_file: str | None, turn: TurnContext | None = None
) -> Command:
    """Build the argv/env/stdin for one turn. ``system_prompt_file`` holds ``spec.system_prompt``."""
    if spec.cli in PLUGIN_CLIS:
        from argus.runner.clis import plugin

        assert turn is not None
        return plugin(spec.cli).build(spec, binaries, turn)
    binary = binaries.for_cli(spec.cli)
    env: dict[str, str] = {}
    if spec.api_key is not None:
        env[API_KEY_ENV[spec.cli]] = spec.api_key.get_secret_value()
    if spec.cli == "claude":
        argv = [
            binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--disallowedTools",
            "*",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if spec.api_key is not None:
            argv.append("--bare")
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if system_prompt_file:
            argv += ["--system-prompt-file", system_prompt_file]
        if not spec.persist:
            argv.append("--no-session-persistence")
        elif spec.session_id:
            argv += ["--resume" if spec.resume else "--session-id", spec.session_id]
        return Command(argv, env, spec.prompt)
    if spec.cli == "codex":
        argv = [
            binary,
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-c",
            "approval_policy=never",
            "-c",
            "web_search=disabled",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        if spec.model:
            argv += ["-m", spec.model]
        if not spec.persist:
            argv.append("--ephemeral")
        # Codex has no system-prompt flag in exec mode: the API folds instructions into the prompt.
        prompt = spec.prompt
        if spec.system_prompt and not spec.resume:
            prompt = f"<system_instructions>\n{spec.system_prompt}\n</system_instructions>\n\n{prompt}"
        if spec.resume and spec.session_id:
            argv += ["resume", spec.session_id, "-"]
        else:
            argv.append("-")
        return Command(argv, env, prompt)
    # gemini: headless because stdin is a pipe; the prompt is read from stdin.
    argv = [binary, "--output-format", "stream-json", "--skip-trust", "--approval-mode", "default"]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.persist and spec.session_id:
        argv += ["--resume" if spec.resume else "--session-id", spec.session_id]
    if system_prompt_file:
        env["GEMINI_SYSTEM_MD"] = system_prompt_file
    return Command(argv, env, spec.prompt)


# Read-only status probes (documented commands). Gemini has no status command: the runner
# reports whether a key is configured or the CLI's own login state exists, without reading it.
def status_argv(cli: str, binaries: Binaries) -> list[str] | None:
    if cli == "claude":
        return [binaries.claude, "auth", "status"]
    if cli == "codex":
        return [binaries.codex, "login", "status"]
    return None


def version_argv(cli: str, binaries: Binaries) -> list[str]:
    return [binaries.for_cli(cli), "--version"]


LOGIN_COMMANDS: dict[str, str] = {
    "claude": "claude auth login",
    "codex": "codex login --device-auth",
    "gemini": "gemini",
}


# ---------------------------------------------------------------------------------------- sign-in
# How each CLI's own login command is run for the in-app sign-in (argus/runner/login.py). Checked
# against the pinned versions: claude 2.1.308 prints its authorize URL and reads the code from a
# pipe; codex 0.163.0 prints a device URL and one-time code and polls; gemini 0.65.0 refuses manual
# sign-in without a terminal, so it gets a PTY, ``NO_BROWSER`` and the Google sign-in preselected,
# and is stopped once it has written its credentials file.

DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,6}\b")
LOGIN_CODE_RE = re.compile(r"^[A-Za-z0-9._~#/+=\-]{1,512}$")


@dataclass(frozen=True)
class LoginSpec:
    cli: str
    args: tuple[str, ...]  # after the binary
    flow: Literal["device_code", "paste_code"]
    url_hosts: tuple[str, ...]  # a relayed link must be on one of these hosts (or a subdomain)
    method_args: dict[str, tuple[str, ...]] = field(default_factory=dict)  # method -> args instead
    pty: bool = False
    env: dict[str, str] = field(default_factory=dict)
    paste_prompt: re.Pattern[str] | None = None  # the CLI waits for a pasted code when this appears
    success_file: str | None = None  # relative to HOME: success once written (CLIs that never exit)
    reset_files: tuple[str, ...] = ()  # removed before a new sign-in (else the CLI reuses the old one)
    settings: tuple[str, dict[str, Any]] | None = None  # (file relative to HOME, keys to merge in)
    # PTY only: fixed key presses for menus the CLI shows before its link, each sent once when its
    # pattern first appears (e.g. Enter to keep a default). Never user input.
    answers: tuple[tuple[re.Pattern[str], str], ...] = ()

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(self.method_args)

    def argv(self, binaries: Binaries, method: str | None = None) -> list[str]:
        if self.method_args:
            if method not in self.method_args:
                raise ValueError("unknown sign-in method")
            return [binaries.for_cli(self.cli), *self.method_args[method]]
        if method:
            raise ValueError("this CLI has a single sign-in method")
        return [binaries.for_cli(self.cli), *self.args]


LOGIN_SPECS: dict[str, LoginSpec] = {
    "claude": LoginSpec(
        cli="claude",
        args=("auth", "login", "--claudeai"),
        flow="paste_code",
        url_hosts=("claude.com", "claude.ai", "anthropic.com"),
        paste_prompt=re.compile(r"Paste code here(?: if prompted)?\s*>?", re.I),
    ),
    "codex": LoginSpec(
        cli="codex",
        args=("login", "--device-auth"),
        flow="device_code",
        url_hosts=("auth.openai.com",),
    ),
    "gemini": LoginSpec(
        cli="gemini",
        args=("--skip-trust",),
        flow="paste_code",
        url_hosts=("accounts.google.com",),
        pty=True,
        env={"NO_BROWSER": "true", "TERM": "xterm-256color"},
        paste_prompt=re.compile(r"Enter the authorization code:?", re.I),
        success_file=".gemini/oauth_creds.json",
        reset_files=(".gemini/oauth_creds.json", ".gemini/google_accounts.json"),
        settings=(".gemini/settings.json", {"security": {"auth": {"selectedType": "oauth-personal"}}}),
    ),
}

# Sign-out: (argv after the binary, or None) and files under HOME to remove.
LOGOUT_SPECS: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...]]] = {
    "claude": (("auth", "logout"), ()),
    "codex": (("logout",), ()),
    "gemini": (None, (".gemini/oauth_creds.json", ".gemini/google_accounts.json")),
}
