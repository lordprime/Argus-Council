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
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

CliName = Literal["claude", "codex", "gemini"]
CLI_NAMES: tuple[CliName, ...] = ("claude", "codex", "gemini")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Model ids and aliases: "sonnet", "claude-opus-5-5[1m]", "gpt-5.5-codex", "gemini-2.5-pro".
# No leading dash (could be read as a flag), no whitespace, no shell or path metacharacters.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,99}$")
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
        if not UUID_RE.match(v):
            raise ValueError("session_id must be a UUID")
        return v.lower()

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
        if self.cli == "codex" and self.session_id and not self.resume:
            raise ValueError("codex assigns its own session ids; pass session_id only to resume")
        return self


@dataclass
class Command:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: str = ""


@dataclass(frozen=True)
class Binaries:
    claude: str = "claude"
    codex: str = "codex"
    gemini: str = "gemini"

    def for_cli(self, cli: str) -> str:
        return getattr(self, cli)


def build_command(spec: InvokeSpec, *, binaries: Binaries, system_prompt_file: str | None) -> Command:
    """Build the argv/env/stdin for one turn. ``system_prompt_file`` holds ``spec.system_prompt``."""
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
