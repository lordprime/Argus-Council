"""GitHub Copilot CLI (``copilot``, npm ``@github/copilot`` 1.0.108).

Prompt mode because stdin is a pipe: the prompt is read from stdin and ``--output-format json``
prints one JSON event per line (``session.start``, ``assistant.message_delta``,
``assistant.message``, ``session.error``, ``model.call_failure``, a final ``result``). Token usage is
not on stdout; ``--usage-output-file`` writes the session's usage as JSON into the turn directory
and the runner passes it on as one ``argus.result_file`` event.

Every tool of its own is turned off, enforced by argv only (Copilot has no local admin policy file;
organisation policy comes from GitHub per account):

* ``--available-tools=`` (no tool is offered to the model) plus ``--deny-tool`` for every
  permission kind (``shell``, ``write``, ``read``, ``url``, ``memory``). A deny always wins, even
  over ``--allow-all``, and prompt mode denies anything that would ask;
* ``--no-ask-user``, ``--disable-builtin-mcps`` (no GitHub MCP server), ``--no-custom-instructions``
  (no AGENTS.md / CLAUDE.md), ``--disallow-temp-dir``, ``COPILOT_PLUGIN_DIR_ONLY=true`` (no plugin
  discovery) and ``COPILOT_DISABLE_LOGIN_SHELL_ENV=1``;
* never ``--allow-all*``, ``--yolo``, ``--autopilot``, ``--trust-folder`` or ``--enable-memory``;
* the runner kills the turn if Copilot reports a tool list, an MCP server's tools or a tool run.

The user's ``~/.copilot`` is runner-owned (per-user HOME), so its ``settings.json`` hooks and
``mcp-config.json`` can only hold what Argus wrote. ``--sync-session=off`` keeps the history off the
GitHub account; ``--disable-client-telemetry`` and ``COPILOT_TELEMETRY_DISABLED`` turn Copilot's own
telemetry off; ``--no-auto-update`` / ``COPILOT_AUTO_UPDATE=false`` keep the pinned version.

There is no system-prompt flag: the system prompt is folded into the prompt on a new session, as for
Codex. Sessions: ``--session-id <uuid>`` both creates a session with the id Argus picked and resumes
it. Copilot has no one-shot mode; a turn with ``persist`` false gets no ``--session-id`` and is not
recorded by Argus (Copilot still keeps it under ``~/.copilot/session-state``).

Sign-in: ``copilot login --device-code`` prints ``To authenticate, visit
https://github.com/login/device and enter code XXXX-XXXX`` and polls. Without a system keychain
(the runner image has none) the token can only be stored in plain text in ``~/.copilot/config.json``
(Copilot's own state file: "signed-in accounts, installed plugins and marketplaces"). Prompt-mode
login asks for that consent only on a terminal and otherwise refuses to save the token, so the
sign-in first sets ``storeTokenPlaintext: true`` in ``~/.copilot/settings.json``. Sign-out removes
``config.json`` (1.0.108 has no ``logout`` subcommand); status is "a sign-in is stored" while it
exists. A token in ``COPILOT_GITHUB_TOKEN`` (fine-grained PAT with "Copilot Requests") takes
precedence over a stored sign-in.
"""

from __future__ import annotations

import os
from typing import Any

from argus.runner.clis import CliPlugin, key_env
from argus.runner.specs import (
    EFFORTS,
    MODEL_RE,
    Binaries,
    Command,
    InvokeSpec,
    LoginSpec,
    TurnContext,
    session_ok,
)

USAGE_FILE = "copilot-usage.json"

# Fixed environment for every turn (the runner adds PATH/HOME/LANG/proxy variables itself).
ENV: dict[str, str] = {
    "COPILOT_AUTO_UPDATE": "false",
    "COPILOT_PLUGIN_DIR_ONLY": "true",
    "COPILOT_DISABLE_LOGIN_SHELL_ENV": "1",
    "COPILOT_DISABLE_RESUME_AUTO_CD": "1",
    "COPILOT_TELEMETRY_DISABLED": "true",
}

# Permission kinds of `--deny-tool` (`copilot help permissions`). A deny beats every allow.
DENIED_KINDS: tuple[str, ...] = ("shell", "write", "read", "url", "memory")


def build(spec: InvokeSpec, binaries: Binaries, turn: TurnContext) -> Command:
    usage_file = os.path.join(turn.turn_dir, USAGE_FILE)
    argv = [
        binaries.copilot,
        "--output-format",
        "json",
        "--stream",
        "on",
        "--no-color",
        "--no-auto-update",
        "--no-custom-instructions",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "--disallow-temp-dir",
        "--available-tools=",
        *(f"--deny-tool={kind}" for kind in DENIED_KINDS),
        "--sync-session=off",
        "--disable-client-telemetry",
        "--usage-output-file",
        usage_file,
    ]
    if spec.model:
        if not MODEL_RE.match(spec.model):  # InvokeSpec checks it too; argv never trusts it
            raise ValueError("invalid model name")
        argv += ["--model", spec.model]
    if spec.effort:
        if spec.effort not in EFFORTS:  # Copilot accepts none..max; Argus sends low..max
            raise ValueError("invalid effort")
        argv += ["--reasoning-effort", spec.effort]
    if spec.persist and spec.session_id:
        if not session_ok("copilot", spec.session_id):
            raise ValueError("invalid session_id")
        # Creates a session with this UUID, or resumes it when it exists.
        argv += ["--session-id", spec.session_id]
    env = dict(ENV)
    if spec.api_key is not None:
        env |= key_env("copilot", spec.key_provider, spec.api_key.get_secret_value(), spec.model)
    # No system-prompt flag: fold the instructions into the prompt of a new session (as Codex).
    prompt = spec.prompt
    if spec.system_prompt and not spec.resume:
        prompt = f"<system_instructions>\n{spec.system_prompt}\n</system_instructions>\n\n{prompt}"
    return Command(argv=argv, env=env, stdin=prompt, result_file=usage_file)


def _data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def session_from_event(event: dict[str, Any]) -> str | None:
    """The session id Copilot reports (the one Argus chose, or its own when none was given)."""
    kind = event.get("type")
    if kind == "session.start":
        sid = _data(event).get("sessionId")
    elif kind == "result":
        sid = event.get("sessionId")
    else:
        return None
    return sid.lower() if isinstance(sid, str) and session_ok("copilot", sid) else None


# Keys a tool list could appear under. 1.0.108's session.start and session.tools_updated carry
# none (only the model), so these are read tolerantly in case a later version adds one.
_TOOL_LIST_KEYS = ("tools", "availableTools", "toolNames", "toolList")


def tools_reported(event: dict[str, Any]) -> list[Any] | None:
    """Anything showing Copilot had tools of its own this turn (the runner then kills the turn)."""
    kind = event.get("type")
    data = _data(event)
    if kind in ("session.start", "session.tools_updated"):
        for key in _TOOL_LIST_KEYS:
            tools = data.get(key)
            if isinstance(tools, list):
                return tools
        return None
    if kind == "tool.execution_start":
        return [str(data.get("toolName") or "tool")[:100]]
    if kind == "session.mcp_server_tools_loaded":
        count = data.get("toolCount")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return [f"mcp:{str(data.get('serverName') or 'server')[:80]}"]
    return None


# `copilot login --device-code` (GitHub OAuth device flow, no terminal needed). It prints the
# verification URL and code on one line, polls, then prints "Signed in successfully as ..." and
# exits 0. Without a keychain it only saves the token in plain text when `storeTokenPlaintext` is
# set (or a terminal user answers "y"), so the setting is merged into settings.json first.
LOGIN = LoginSpec(
    cli="copilot",
    args=("login", "--device-code"),
    flow="device_code",
    url_hosts=("github.com",),
    env={
        "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_DISABLE_LOGIN_SHELL_ENV": "1",
        "COPILOT_TELEMETRY_DISABLED": "true",
    },
    settings=(".copilot/settings.json", {"storeTokenPlaintext": True}),
)

# Copilot's own state file under COPILOT_HOME (~/.copilot): signed-in accounts and their plain-text
# tokens when no keychain exists, plus trusted folders and installed plugins.
CREDENTIAL_FILE = ".copilot/config.json"


PLUGIN = CliPlugin(
    name="copilot",
    label="GitHub Copilot CLI",
    build=build,
    suggested_models=(
        "default",
        "auto",
        "claude-sonnet-5.5",
        "claude-opus-5.5",
        "gpt-6.1-sol",
        "gpt-5.3-codex",
        "gemini-4-argon",
    ),
    key_env="COPILOT_GITHUB_TOKEN",
    supports_effort=True,
    # --usage-output-file reports the whole session (it is rebuilt from checkpoints on resume).
    usage_is_session_total=True,
    login=LOGIN,
    logout=(None, (CREDENTIAL_FILE,)),
    status_files=(CREDENTIAL_FILE,),
    session_from_event=session_from_event,
    tools_reported=tools_reported,
)
