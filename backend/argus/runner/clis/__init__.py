"""Vendor CLIs added after Claude Code, Codex and the Gemini CLI, one module each.

Each module ``argus/runner/clis/<name>.py`` defines ``PLUGIN: CliPlugin``: how the runner builds one
turn (fixed argv, environment, per-turn files), how it signs in and out, how it reports its status,
and the hooks the runner uses on the CLI's output (session id read-back and the check that the CLI
started with no tools of its own). The same rules as ``argus/runner/specs.py`` apply: no shell,
every value in argv checked against a strict pattern, the prompt on stdin, every tool of the CLI's
own turned off and enforced by the operator (flag, environment or a root-owned policy file in the
runner image), never only by user config.

The API side parses each CLI's events with ``argus/providers/cli/vendors/<name>.py``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from argus.runner.specs import (
    LOGIN_SPECS,
    LOGOUT_SPECS,
    PLUGIN_CLIS,
    Binaries,
    Command,
    InvokeSpec,
    LoginSpec,
    TurnContext,
)


@dataclass(frozen=True)
class CliPlugin:
    name: str
    label: str
    build: Callable[[InvokeSpec, Binaries, TurnContext], Command]
    suggested_models: tuple[str, ...] = ("default",)
    # API key: one env var, or (harness CLIs) a provider choice, each mapping to env vars.
    # Values may contain "{key}" (the user's key) and "{model}" (the seat's model, or "").
    key_env: str = ""
    key_providers: dict[str, dict[str, str]] = field(default_factory=dict)
    # Effort levels the CLI accepts ("low"...), passed through when the seat sets one.
    supports_effort: bool = False
    # True when the CLI reports token usage for the whole session, not just this turn (the
    # adapter then counts the difference from the seat's previous turn).
    usage_is_session_total: bool = False
    login: LoginSpec | None = None
    # Sign-out: (argv after the binary, or None) and files under HOME to remove.
    logout: tuple[tuple[str, ...] | None, tuple[str, ...]] = (None, ())
    # Status: argv after the binary and a parser of (exit code, output) -> status dict, or files
    # under HOME whose presence means "a sign-in is stored (not verified)".
    status_args: tuple[str, ...] | None = None
    parse_status: Callable[[int | None, str], dict[str, Any]] | None = None
    status_files: tuple[str, ...] = ()
    # Output hooks (runner side).
    session_from_event: Callable[[dict[str, Any]], str | None] = lambda event: None
    tools_reported: Callable[[dict[str, Any]], list[Any] | None] = lambda event: None


def plugin(cli: str) -> CliPlugin:
    if cli not in PLUGIN_CLIS:
        raise KeyError(cli)
    return importlib.import_module(f"argus.runner.clis.{cli}").PLUGIN


def login_spec_for(cli: str) -> LoginSpec | None:
    if cli in LOGIN_SPECS:
        return LOGIN_SPECS[cli]
    return plugin(cli).login if cli in PLUGIN_CLIS else None


def logout_spec_for(cli: str) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    if cli in LOGOUT_SPECS:
        return LOGOUT_SPECS[cli]
    return plugin(cli).logout if cli in PLUGIN_CLIS else (None, ())


def key_env(cli: str, provider: str | None, key: str, model: str | None) -> dict[str, str]:
    """Environment carrying a user's API key for a plugin CLI (empty when the provider is unknown)."""
    p = plugin(cli)
    if p.key_providers:
        template = p.key_providers.get(provider or "")
        if template is None:
            return {}
        return {k: v.replace("{key}", key).replace("{model}", model or "") for k, v in template.items()}
    return {p.key_env: key} if p.key_env else {}
