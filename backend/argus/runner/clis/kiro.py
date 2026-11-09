"""Kiro CLI 3.1.0 (``kiro-cli``), driven over its Agent Client Protocol mode.

Kiro's ``chat --output-format stream-json`` schema is undocumented, so the runner speaks the
documented Agent Client Protocol instead (``kiro-cli acp``, JSON-RPC 2.0 on stdio; see
``argus/runner/acp.py``). Docs: https://kiro.dev/docs/cli/acp/ and
https://kiro.dev/docs/reference/cli-commands/ (``kiro-cli acp``).

Argv: ``kiro-cli acp --agent-engine v2 --agent argus-seat [--model M]``. ``--agent`` and ``--model``
are V2-only flags of ``kiro-cli acp``; naming the V2 harness explicitly keeps a saved
``acp.agentEngine`` setting from switching to V3, where those flags are rejected.

Kiro's own tools are off, enforced by the operator in four layers:

* the image installs ``/etc/kiro/managed-settings.json`` (root-owned) denying every capability
  (``fs_read`` ... ``sandbox_network``). A deny there always wins, users cannot override it, and a
  malformed file fails closed (every tool call denied). Source:
  ``backend/docker/runner/policies/kiro-managed-settings.json``;
* the per-turn custom agent ``argus-seat`` has ``tools: []``, ``allowedTools: []``, no MCP servers,
  ``includeMcpJson: false``, no resources and no hooks. It is written into the turn's working
  directory (a workspace agent, ``<cwd>/.kiro/agents/``) so concurrent seats of one user never
  share an agent file and so never see each other's system prompt;
* settings ``chat.enableKnowledge`` / ``chat.enableCodeIntelligence`` off and
  ``chat.disableInheritingDefaultResources`` on (no steering, skills or AGENTS.md), telemetry off,
  in ``$KIRO_HOME/settings/cli.json``;
* the Agent Client Protocol client refuses every ``session/request_permission`` and offers no file system or
  terminal. Never ``--trust-all-tools``/``--trust-tools`` and never ``--cloud``.

Defence in depth: a tool call of a real tool kind (read, edit, shell...), an MCP server coming up
or the custom agent failing to load is reported by :func:`_tools_reported`, and the runner kills
the turn with ``tools_not_disabled``.

Sessions: Kiro assigns UUIDs (``session/new``); resume is ``session/load``. ``KIRO_HOME`` is the
user's ``~/.kiro`` (Kiro's default), which holds sessions, settings and sign-ins, so the in-app
sign-in (run with the same HOME) and the turns agree. Kiro saves every session; a one-shot turn
(``persist`` false) is simply not recorded by Argus.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from argus.runner.clis import CliPlugin, key_env
from argus.runner.specs import UUID_RE, Binaries, Command, InvokeSpec, LoginSpec, TurnContext

AGENT_NAME = "argus-seat"
# Settings written to $KIRO_HOME/settings/cli.json before each turn (flat dotted keys).
SETTINGS: dict[str, Any] = {
    "chat.enableKnowledge": False,
    "chat.enableCodeIntelligence": False,
    "chat.disableInheritingDefaultResources": True,
    "telemetry.enabled": False,
}
# Every capability of Kiro's permission model (https://kiro.dev/docs/permissions/).
CAPABILITIES: tuple[str, ...] = (
    "fs_read",
    "fs_write",
    "shell",
    "web_fetch",
    "web_search",
    "mcp",
    "subagent",
    "skill",
    "power",
    "context",
    "diagnostics",
    "sandbox_network",
)
# Tool kinds that mean the model had a real tool (think/other cover Kiro's built-in planning).
_TOOL_KINDS = frozenset({"read", "edit", "delete", "move", "search", "execute", "fetch"})
_AGENT_FAILURE_RE = re.compile(r"customAgent/(?:not_found|config_error)$")
_MCP_UP_RE = re.compile(r"mcp/server_initialized$")


def _agent(turn: TurnContext) -> dict[str, Any]:
    agent: dict[str, Any] = {
        "name": AGENT_NAME,
        "description": "Argus council seat: answers in text, no tools.",
        "tools": [],
        "allowedTools": [],
        "mcpServers": {},
        "includeMcpJson": False,
        "resources": [],
        "hooks": {},
    }
    if turn.system_prompt_file:
        agent["prompt"] = "file://" + os.path.abspath(turn.system_prompt_file)
    return agent


def _build(spec: InvokeSpec, binaries: Binaries, turn: TurnContext) -> Command:
    kiro_home = os.path.join(turn.home, ".kiro")
    argv = [binaries.kiro, "acp", "--agent-engine", "v2", "--agent", AGENT_NAME]
    if spec.model:  # checked against specs.MODEL_RE by InvokeSpec (no leading dash)
        argv += ["--model", spec.model]
    env = {"KIRO_HOME": kiro_home}
    if spec.api_key is not None:
        env |= key_env("kiro", spec.key_provider, spec.api_key.get_secret_value(), spec.model)
    files = {
        os.path.join(turn.cwd, ".kiro", "agents", f"{AGENT_NAME}.json"): json.dumps(_agent(turn), indent=2),
        os.path.join(kiro_home, "settings", "cli.json"): json.dumps(SETTINGS, indent=2),
    }
    return Command(argv=argv, env=env, stdin=spec.prompt, files=files, protocol="acp")


def _session_from_event(event: dict[str, Any]) -> str | None:
    if event.get("type") != "acp.session":
        return None
    sid = event.get("sessionId")
    return sid.lower() if isinstance(sid, str) and UUID_RE.match(sid) else None


def _tools_reported(event: dict[str, Any]) -> list[Any] | None:
    """Anything showing Kiro had tools of its own this turn (the runner then kills the turn)."""
    kind = event.get("type")
    if kind == "acp.update":
        update = event.get("update") if isinstance(event.get("update"), dict) else {}
        if update.get("sessionUpdate") == "tool_call" and update.get("kind") in _TOOL_KINDS:
            return [str(update.get("title") or update.get("kind"))[:100]]
        return None
    if kind == "acp.notification":
        method = str(event.get("method") or "")
        if _MCP_UP_RE.search(method):
            return ["mcp"]
        if _AGENT_FAILURE_RE.search(method):
            return ["default agent"]  # argus-seat did not load; Kiro's default agent has tools
    return None


# Status: `kiro-cli whoami --format json`. The JSON fields are not documented, so only the sign-in
# method is read, from whichever of these keys is present; account names and emails are dropped.
_METHOD_KEYS = (
    "accountType",
    "authMethod",
    "authenticationMethod",
    "loginMethod",
    "method",
    "type",
    "provider",
)


def _method(value: str) -> str | None:
    v = value.lower()
    for needle, method in (
        ("builder", "builder_id"),
        ("identity", "identity_center"),
        ("iam", "identity_center"),
        ("sso", "identity_center"),
        ("google", "google"),
        ("github", "github"),
        ("social", "social"),
        ("api", "api_key"),
    ):
        if needle in v:
            return method
    return None


def _parse_status(code: int | None, output: str) -> dict[str, Any]:
    text = output.strip()
    if code is None:
        return {"logged_in": None, "method": None, "verified": False, "detail": text[:200]}
    data: Any = None
    start = text.find("{")
    if start >= 0:
        try:
            data = json.loads(text[start:])
        except ValueError:
            data = None
    method: str | None = None
    if isinstance(data, dict):
        for key in _METHOD_KEYS:
            if isinstance(data.get(key), str):
                method = _method(data[key])
                if method:
                    break
    elif code == 0:
        method = _method(text.splitlines()[0]) if text else None
    lowered = text.lower()
    if code == 0 and "not logged in" not in lowered:
        return {"logged_in": True, "method": method, "verified": True, "detail": ""}
    if "not logged in" in lowered or "log in" in lowered or "login" in lowered or "logged out" in lowered:
        return {"logged_in": False, "method": None, "verified": True, "detail": ""}
    return {
        "logged_in": None,
        "method": None,
        "verified": False,
        "detail": "Kiro could not report its sign-in state.",
    }


# Sign-in: `kiro-cli login --use-device-flow` prints a verification URL and a one-time code and
# polls until the user approves it. `--license free` selects the free sign-ins (Builder ID, Google,
# GitHub) so the CLI never stops at its interactive method picker; `--social` picks Google or GitHub.
# Builder ID verifies at view.awsapps.com (AWS IAM Identity Center's device page); social sign-in
# goes through Kiro's own pages.
LOGIN = LoginSpec(
    cli="kiro",
    args=("login", "--license", "free", "--use-device-flow"),
    flow="device_code",
    url_hosts=("view.awsapps.com", "aws.amazon.com", "kiro.dev"),
    method_args={
        "builder_id": ("login", "--license", "free", "--use-device-flow"),
        "google": ("login", "--license", "free", "--social", "google", "--use-device-flow"),
        "github": ("login", "--license", "free", "--social", "github", "--use-device-flow"),
    },
)


PLUGIN = CliPlugin(
    name="kiro",
    label="Kiro CLI",
    build=_build,
    # Kiro's model ids come from `kiro-cli chat --list-models`; only this one is quoted in its docs.
    suggested_models=("default", "claude-sonnet-4.6"),
    key_env="KIRO_API_KEY",
    supports_effort=False,
    login=LOGIN,
    logout=(("logout",), ()),
    status_args=("whoami", "--format", "json"),
    parse_status=_parse_status,
    session_from_event=_session_from_event,
    tools_reported=_tools_reported,
)
