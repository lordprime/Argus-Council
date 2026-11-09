"""Mistral Vibe (PyPI ``mistral-vibe`` 3.1.0, binary ``vibe``) as a council seat.

One turn is ``vibe -p`` in headless mode with the prompt on stdin:

* ``--output streaming`` prints each history entry once it is completed (no token deltas).
* ``--agent ask``: the builtin agent that asks before a tool runs; headless, nobody can approve.
* ``--disabled-tools '*'`` (one argv element, no shell): the fnmatch denylist is final in Vibe's
  tool manager, covers builtin, MCP, connector and provided tools, and is concatenated across
  every config layer, so no user, agent-profile or admin layer can remove it. With no tool left in
  the catalogue the unified runtime sets each of its builtins (file system, process, skill) to
  ``deny``. Headless Vibe also always disables ``ask_user_question`` and ``cron``.
* ``VIBE_ENABLE_SUBAGENTS=false`` and ``VIBE_ENABLE_BACKGROUND_PROCESSES=false`` (the environment
  layer sits above user TOML), ``VIBE_ENABLE_TELEMETRY=false``, and ``VIBE_HOME`` in the user's
  HOME. Never ``--auto-approve``/``--yolo``/``--smart-approve`` and never ``--trust`` (without it
  the working folder's ``.vibe/config.toml`` is not loaded).
* ``--output-dir`` in the turn's private directory: Vibe writes ``export.json`` there on every
  exit, which carries the session id (Vibe picks it), usage, cost, the outcome and the error code.
  The runner reads it after exit and emits it as one ``argus.result_file`` event.

Residual: the harness keeps its ``run_typescript`` program tool. It reaches tools only through
the same tool modes, all ``deny`` here, so it can compute but cannot touch files, shell or network.
:func:`_tools_reported` stops the turn if any other tool is ever seen executing.

Sign-in: API key only (``MISTRAL_API_KEY``). Vibe's browser sign-in lives in its interactive
``--setup`` wizard and only produces a Mistral API key. No status or logout command exists.
"""

from __future__ import annotations

from typing import Any

from argus.runner.clis import CliPlugin, key_env
from argus.runner.specs import MODEL_RE, Binaries, Command, InvokeSpec, TurnContext, session_ok

# The result file Vibe writes under --output-dir (vibe/cli-rust/src/run_export.rs EXPORT_FILENAME).
OUTPUT_DIR = "vibe-out"
EXPORT_FILE = "export.json"
# The program tool the harness always offers (compute-only once every other tool is denied).
SANDBOX_TOOL = "run_typescript"


def _build(spec: InvokeSpec, binaries: Binaries, turn: TurnContext) -> Command:
    out_dir = f"{turn.turn_dir}/{OUTPUT_DIR}"
    argv = [
        binaries.vibe,
        "-p",  # no text: the prompt is read from stdin
        "--output",
        "streaming",
        "--agent",
        "ask",
        "--disabled-tools",
        "*",
    ]
    if turn.system_prompt_file:
        argv += ["--system-prompt-file", turn.system_prompt_file]
    argv += ["--output-dir", out_dir]
    if spec.resume:
        if not session_ok("vibe", spec.session_id):
            raise ValueError("invalid session_id for vibe")
        assert spec.session_id is not None
        argv += ["--resume", spec.session_id]
    elif spec.model:
        # --model applies to new sessions only (Vibe rejects it with --resume: the model is
        # pinned to the session at its first message).
        if not MODEL_RE.match(spec.model):
            raise ValueError("invalid model name")
        argv += ["--model", spec.model]
    env = {
        "VIBE_HOME": f"{turn.home}/.vibe",
        "VIBE_ENABLE_SUBAGENTS": "false",
        "VIBE_ENABLE_BACKGROUND_PROCESSES": "false",
        "VIBE_ENABLE_TELEMETRY": "false",
    }
    if not spec.persist:
        # One-shot: nothing written to Vibe's session log (the env layer's nested field).
        env["VIBE_SESSION_LOGGING__ENABLED"] = "false"
    if spec.api_key is not None:
        env |= key_env("vibe", spec.key_provider, spec.api_key.get_secret_value(), spec.model)
    return Command(argv, env, spec.prompt, result_file=f"{out_dir}/{EXPORT_FILE}")


def _session_from_event(event: dict[str, Any]) -> str | None:
    """Vibe assigns the session id; it is only in export.json (the runner validates it)."""
    if event.get("type") != "argus.result_file":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _tools_reported(event: dict[str, Any]) -> list[Any] | None:
    """No Vibe event lists the tools offered, so check the next best thing: a completed ``effect``
    entry (a tool call) that actually executed. With every tool denied only the compute-only
    ``run_typescript`` sandbox can run; anything else running means the denylist did not hold."""
    if event.get("type") != "effect":
        return None
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    state = event.get("state") if isinstance(event.get("state"), dict) else {}
    tool = str(detail.get("toolName") or detail.get("tool_name") or "")
    if tool == SANDBOX_TOOL:
        return None
    if state.get("decision") == "execute" or (
        state.get("status") in ("running", "completed") and state.get("decision") != "skip"
    ):
        return [tool or "unknown"]
    return None


PLUGIN = CliPlugin(
    name="vibe",
    label="Mistral Vibe",
    build=_build,
    suggested_models=("default", "mistral-medium-3.5"),
    key_env="MISTRAL_API_KEY",
    supports_effort=False,
    login=None,
    session_from_event=_session_from_event,
    tools_reported=_tools_reported,
)
