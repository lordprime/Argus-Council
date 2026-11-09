"""Qwen Code (``qwen``, npm ``@qwen-code/qwen-code`` 0.27.1), a Gemini CLI fork.

Headless because stdin is a pipe: the prompt is read from stdin and the output is Claude-style
``stream-json`` (``system/init``, ``stream_event``, ``assistant``, ``result``). Every tool of its own
is turned off in layers:

* the image installs a root-owned system settings file at ``/etc/qwen-code/settings.json``
  (``backend/docker/runner/policies/qwen-settings.json``). Qwen merges it last, so it has the
  highest precedence, and ``permissions.deny`` is union-merged (no lower scope can remove an
  entry). It denies and disables all 71 built-in tool names of 0.27.1 (there is no ``*`` wildcard
  for built-in tools, so the list must be re-checked on every version bump) plus ``mcp__*``,
  excludes every MCP server, and turns web search, hooks, telemetry and auto-update off;
* ``--approval-mode default`` (anything that would ask is denied when headless; never ``yolo``),
  ``--safe-mode`` (no context files, hooks, extensions, skills or MCP servers) and
  ``--max-tool-calls 0`` (the first tool call aborts the run with exit code 55);
* ``ENABLE_WEB_SEARCH=false`` (the env override wins over settings, and the runner owns the env);
* the runner kills the turn if ``system/init.tools`` is not empty.

Never ``--bare``: bare mode re-enables read_file, edit, notebook_edit and run_shell_command. Never
``--yolo``. The system prompt comes from a file through ``QWEN_SYSTEM_MD``. The key is passed in the
environment (``OPENAI_API_KEY``/``OPENAI_BASE_URL``/``OPENAI_MODEL``, the env-only path that selects
the OpenAI-compatible auth type), never with ``--openai-api-key`` in argv.
"""

from __future__ import annotations

from typing import Any

from argus.runner.clis import CliPlugin, key_env
from argus.runner.specs import MODEL_RE, Binaries, Command, InvokeSpec, TurnContext, session_ok

# OpenAI-compatible endpoints per key provider. Alibaba Coding Plan / Token Plan keys are not
# offered: their terms forbid automated and backend use.
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Qwen only takes the OpenAI-compatible path from env when OPENAI_MODEL is set, so a seat without
# a model gets the provider's coding model.
DEFAULT_MODELS: dict[str, str] = {
    "dashscope": "qwen3-coder-plus",
    "openrouter": "qwen/qwen3-coder-plus",
}


def _key_template(base_url: str) -> dict[str, str]:
    return {"OPENAI_API_KEY": "{key}", "OPENAI_BASE_URL": base_url, "OPENAI_MODEL": "{model}"}


def build(spec: InvokeSpec, binaries: Binaries, turn: TurnContext) -> Command:
    argv = [
        binaries.qwen,
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--approval-mode",
        "default",
        "--safe-mode",
        "--max-tool-calls",
        "0",
    ]
    if spec.model:
        if not MODEL_RE.match(spec.model):  # InvokeSpec checks it too; argv never trusts it
            raise ValueError("invalid model name")
        argv += ["-m", spec.model]
    if not spec.persist:
        argv += ["--chat-recording", "false"]
    elif spec.session_id:
        if not session_ok("qwen", spec.session_id):
            raise ValueError("invalid session_id")
        # Argus picks the UUID of a new session; qwen refuses an id that is already taken.
        argv += ["--resume" if spec.resume else "--session-id", spec.session_id]
    env: dict[str, str] = {"ENABLE_WEB_SEARCH": "false"}
    if turn.system_prompt_file:
        env["QWEN_SYSTEM_MD"] = turn.system_prompt_file
    if spec.api_key is not None:
        keys = key_env("qwen", spec.key_provider, spec.api_key.get_secret_value(), spec.model)
        if keys and not keys.get("OPENAI_MODEL"):
            keys["OPENAI_MODEL"] = DEFAULT_MODELS.get(spec.key_provider or "", "")
        env.update(keys)
    return Command(argv, env, spec.prompt)


def session_from_event(event: dict[str, Any]) -> str | None:
    if event.get("type") == "system" and event.get("subtype") == "init":
        sid = event.get("session_id")
        return sid if isinstance(sid, str) else None
    return None


def tools_reported(event: dict[str, Any]) -> list[Any] | None:
    if event.get("type") == "system" and event.get("subtype") == "init":
        tools = event.get("tools")
        if isinstance(tools, list):
            return tools
        # An init without a list is unexpected: treat it as tools present (fail closed).
        return ["<missing tools list>"]
    return None


PLUGIN = CliPlugin(
    name="qwen",
    label="Qwen Code",
    build=build,
    suggested_models=("default", "qwen3-coder-plus", "qwen3-coder-flash", "qwen3-max"),
    key_providers={
        "dashscope": _key_template(DASHSCOPE_BASE_URL),
        "openrouter": _key_template(OPENROUTER_BASE_URL),
    },
    supports_effort=False,
    # API keys only: Qwen's account sign-in lives in its interactive /auth screen, and the free
    # Qwen OAuth tier ended on 2026-04-15. Status reports "Uses an API key".
    login=None,
    session_from_event=session_from_event,
    tools_reported=tools_reported,
)
