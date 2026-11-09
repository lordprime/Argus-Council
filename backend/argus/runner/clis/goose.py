"""Goose (``goose run``, GitHub release v1.57.0 of github.com/aaif-goose/goose).

Goose is a harness: it has no account of its own and runs a model from whichever provider the
user's API key belongs to. Every tool it has comes from an extension, and both of these turn them
off, set by the operator (argv and the process env), never by a file the user can edit:

* ``--no-profile``: no default extensions and no plugin MCP servers, and nothing is added with
  ``--with-extension``/``--with-builtin``, so the model is offered no tools at all. A resumed
  session reuses its saved extension set, which is empty.
* ``GOOSE_MODE=chat`` ("Chat only, no tool calls"): any tool call the model makes up anyway gets a
  canned "skipped" result instead of running. Environment variables outrank Goose's config file and
  the CLI re-applies the mode on every build, so this also holds on resume.

The provider is pinned in the env (``GOOSE_PROVIDER``) to an HTTP API provider. Never a provider
that wraps another vendor's agent (``claude-code``, ``codex``, ``gemini-cli``, ``cursor-agent``,
``*-acp``): those run an agent with tools of its own. Without a key the turn is refused, so the
provider can never come from Goose's own config file.

Argus picks the session id: a new session is created with ``-n <uuid>`` (a name; Goose's own ids
are ``YYYYMMDD_<n>``) and resumed with ``--resume -n <uuid>``. The id never appears in Goose's
output, so ``session_from_event`` stays unset and the runner keeps the id it was given.
``GOOSE_PATH_ROOT`` keeps Goose's config, data (the sessions database) and state in the user's
HOME, ``GOOSE_DISABLE_KEYRING`` stops it reaching for a system keyring (the key comes from env).
"""

from __future__ import annotations

import os

from argus.runner.clis import CliPlugin, key_env
from argus.runner.specs import Binaries, Command, InvokeSpec, TurnContext, session_ok

# Goose providers that run another vendor's agent (with tools of its own) instead of calling an
# HTTP API (documentation/docs/getting-started/providers.md at v1.57.0). Never selected.
CLI_WRAPPING_PROVIDERS: frozenset[str] = frozenset(
    {
        "claude-code",
        "claude_code",
        "codex",
        "gemini-cli",
        "gemini_cli",
        "cursor-agent",
        "cursor_agent",
        "claude-acp",
        "codex-acp",
        "copilot-acp",
        "amp-acp",
        "pi-acp",
    }
)

# Provider choice -> env. GOOSE_PROVIDER is Goose's provider id (crates/goose-providers at v1.57.0:
# ANTHROPIC_PROVIDER_NAME "anthropic", OPEN_AI_PROVIDER_NAME "openai", GOOGLE_PROVIDER_NAME "google",
# OPENROUTER_PROVIDER_NAME "openrouter", declarative definitions/mistral.json "mistral"); the key env
# is the one each provider reads.
KEY_PROVIDERS: dict[str, dict[str, str]] = {
    "anthropic": {"ANTHROPIC_API_KEY": "{key}", "GOOSE_PROVIDER": "anthropic", "GOOSE_MODEL": "{model}"},
    "openai": {"OPENAI_API_KEY": "{key}", "GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "{model}"},
    "google": {"GOOGLE_API_KEY": "{key}", "GOOSE_PROVIDER": "google", "GOOSE_MODEL": "{model}"},
    "openrouter": {"OPENROUTER_API_KEY": "{key}", "GOOSE_PROVIDER": "openrouter", "GOOSE_MODEL": "{model}"},
    "mistral": {"MISTRAL_API_KEY": "{key}", "GOOSE_PROVIDER": "mistral", "GOOSE_MODEL": "{model}"},
}

# A turn with tools off is one model call; the cap only bounds a model that keeps inventing tool
# calls (each one skipped in chat mode). Goose's default is 1000.
MAX_TURNS = "4"


def build(spec: InvokeSpec, binaries: Binaries, turn: TurnContext) -> Command:
    if spec.api_key is None:
        # No account of its own: without a key Goose would fall back to its config file's provider.
        raise ValueError("goose needs an API key and its provider")
    env = key_env("goose", spec.key_provider, spec.api_key.get_secret_value(), spec.model)
    provider = env.get("GOOSE_PROVIDER", "")
    if not provider or provider in CLI_WRAPPING_PROVIDERS:
        raise ValueError("unsupported key provider for goose")
    if not env.get("GOOSE_MODEL"):
        env.pop("GOOSE_MODEL", None)  # Goose then uses the provider's default model
    # Set last so nothing above can change them.
    env |= {
        "GOOSE_MODE": "chat",
        "GOOSE_PATH_ROOT": os.path.join(turn.home, ".goose"),
        "GOOSE_DISABLE_KEYRING": "1",
        "GOOSE_TELEMETRY_ENABLED": "false",
        "GOOSE_DISABLE_SESSION_NAMING": "true",
    }
    if turn.system_prompt_file:
        env["GOOSE_SYSTEM_PROMPT_FILE_PATH"] = turn.system_prompt_file
    else:
        env.pop("GOOSE_SYSTEM_PROMPT_FILE_PATH", None)
    argv = [
        binaries.goose,
        "run",
        "--no-profile",
        "--output-format",
        "stream-json",
        "-i",
        "-",  # the prompt comes from stdin
        "--max-turns",
        MAX_TURNS,
    ]
    if spec.persist and spec.session_id:
        if not session_ok("goose", spec.session_id):
            raise ValueError("invalid session_id for goose")
        argv += ["--resume", "-n", spec.session_id] if spec.resume else ["-n", spec.session_id]
    else:
        # One-shot (or no id to find it by later): nothing is written to Goose's session store.
        argv.append("--no-session")
    return Command(argv, env, spec.prompt)


PLUGIN = CliPlugin(
    name="goose",
    label="Goose",
    build=build,
    suggested_models=(
        "default",
        "claude-sonnet-4-5",
        "gpt-6.1-sol",
        "gemini-2.5-pro",
        "mistral-medium-latest",
    ),
    key_providers=KEY_PROVIDERS,
    supports_effort=False,
    usage_is_session_total=True,  # "complete" sums the whole session (get_session_usage_totals)
    login=None,  # no account of its own: a provider API key is the only sign-in
    logout=(None, ()),
    # The session id is Argus's own (``-n <uuid>``), never read back; no event lists tools.
    session_from_event=lambda event: None,
    tools_reported=lambda event: None,
)
