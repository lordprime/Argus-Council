"""A minimal client for the Agent Client Protocol, for CLIs that run as an agent over it.

The Agent Client Protocol (https://agentclientprotocol.com) is Zed's JSON-RPC 2.0 protocol between an
editor and a coding agent, one JSON message per line on the agent's stdin/stdout. It is unrelated to
the "Agent Communication Protocol" Argus removed in 0.1.4. Kiro CLI speaks it as ``kiro-cli acp``.

One turn is one short conversation with a freshly started agent process:

1. ``initialize`` (protocol version 1). The client offers nothing: no file system, no terminal.
2. ``session/new`` ``{cwd, mcpServers: []}``, or ``session/load`` ``{sessionId, cwd, mcpServers: []}``
   to resume. While a session loads the agent replays its history as ``session/update``
   notifications; those are dropped so old text is not counted again.
3. ``session/prompt`` ``{sessionId, prompt: [{type: "text", text}]}``. The agent streams
   ``session/update`` notifications and finally answers with ``{stopReason}``.

Requests from the agent are answered so the agent never waits on Argus: every
``session/request_permission`` is refused (the ``reject_once``/``reject_always`` option the agent
offered, else the ``cancelled`` outcome), and ``fs/*``, ``terminal/*`` and anything else get a
JSON-RPC "method not allowed" error. The client never runs a tool for the agent.

Events yielded, in order:

* ``{"type": "acp.session", "sessionId": ...}`` once the session id is known;
* ``{"type": "acp.update", "sessionId": ..., "update": {...}}`` for every ``session/update`` of the turn;
* ``{"type": "acp.notification", "method": ..., "params": {...}}`` for the agent's other notifications
  (vendor extensions such as rate-limit notices);
* last, ``{"type": "acp.result", "stopReason": ...}`` or
  ``{"type": "acp.error", "error": {"code", "message"}, "stage": ..., "reason": ...}``.

``reason`` on an error is ``rpc`` (the agent answered with a JSON-RPC error), ``protocol`` (the
agent's answer made no sense), ``eof`` (it closed its output first), ``timeout`` or
``output_too_large``. Lines that are not JSON objects are ignored, lines above the size cap are
skipped, and the prompt is never repeated in an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "argus-council", "title": "Argus Council", "version": "0.1.4"}
CLIENT_CAPABILITIES: dict[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
METHOD_NOT_ALLOWED = -32601
_METHOD_RE = re.compile(r"^[A-Za-z0-9_./\-]{1,128}$")


class _Closed(Exception):
    pass


class _Timeout(Exception):
    pass


class _TooLarge(Exception):
    pass


class _Conn:
    """Newline-delimited JSON-RPC over the agent's stdin/stdout."""

    def __init__(self, proc: asyncio.subprocess.Process, deadline: float) -> None:
        assert proc.stdin is not None and proc.stdout is not None
        self.stdin = proc.stdin
        self.stdout = proc.stdout
        self.deadline = deadline
        self.total = 0
        self.next_id = 0

    async def send(self, message: dict[str, Any]) -> None:
        data = json.dumps({"jsonrpc": "2.0", **message}, separators=(",", ":")) + "\n"
        try:
            self.stdin.write(data.encode())
            await self.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise _Closed from exc

    async def recv(self) -> dict[str, Any]:
        """The next JSON object from the agent. Raises _Closed at EOF."""
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _Timeout
            try:
                line = await asyncio.wait_for(self.stdout.readline(), remaining)
            except TimeoutError:
                raise _Timeout from None
            except ValueError:  # one line above the stream's limit: skipped
                continue
            if not line:
                raise _Closed
            self.total += len(line)
            if self.total > MAX_TOTAL_BYTES:
                raise _TooLarge
            if len(line) > MAX_LINE_BYTES:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # banners and log lines are not part of the protocol
            if isinstance(message, dict):
                return message


def _scrub(text: str, prompt: str) -> str:
    """Never repeat the prompt (or a recognisable line of it) in an error message."""
    if prompt:
        text = text.replace(prompt, "[prompt]")
        for line in sorted({ln.strip() for ln in prompt.splitlines()}, key=len, reverse=True):
            if len(line) >= 12:
                text = text.replace(line, "[prompt]")
    return text


def _rpc_error(error: Any, prompt: str, stage: str) -> dict[str, Any]:
    err = error if isinstance(error, dict) else {}
    code = err.get("code") if isinstance(err.get("code"), int) else None
    message = err.get("message") if isinstance(err.get("message"), str) else "agent request failed"
    out: dict[str, Any] = {"code": code, "message": _scrub(message, prompt)[:500]}
    data = err.get("data")
    if data is not None:
        raw = data if isinstance(data, str) else json.dumps(data, default=str)
        out["data"] = _scrub(raw, prompt)[:500]
    return {"type": "acp.error", "error": out, "stage": stage, "reason": "rpc"}


def _local_error(reason: str, message: str, stage: str) -> dict[str, Any]:
    return {
        "type": "acp.error",
        "error": {"code": None, "message": message},
        "stage": stage,
        "reason": reason,
    }


def _permission_outcome(params: Any) -> dict[str, Any]:
    """Refuse: pick the agent's own reject option, else answer "cancelled" (both are a refusal)."""
    options = params.get("options") if isinstance(params, dict) else None
    for kind in ("reject_once", "reject_always"):
        for option in options if isinstance(options, list) else []:
            if (
                isinstance(option, dict)
                and option.get("kind") == kind
                and isinstance(option.get("optionId"), str)
            ):
                return {"outcome": "selected", "optionId": option["optionId"]}
    return {"outcome": "cancelled"}


def _answer(message: dict[str, Any]) -> dict[str, Any]:
    """The reply to one request from the agent. Argus offers the agent no capabilities."""
    if message.get("method") == "session/request_permission":
        return {"id": message.get("id"), "result": {"outcome": _permission_outcome(message.get("params"))}}
    return {
        "id": message.get("id"),
        "error": {
            "code": METHOD_NOT_ALLOWED,
            "message": "Method not allowed: this client offers no file system, terminal or other tools",
        },
    }


class _Exchange:
    """State shared across the steps of one turn."""

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.session_id: str | None = None
        self.forward_updates = False
        self.response: dict[str, Any] | None = None

    async def call(self, method: str, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Send one request; yield events until its response arrives (left in ``self.response``)."""
        req_id = self.conn.next_id
        self.conn.next_id += 1
        self.response = None
        await self.conn.send({"id": req_id, "method": method, "params": params})
        while True:
            message = await self.conn.recv()
            method_in = message.get("method")
            if isinstance(method_in, str):
                if "id" in message:  # a request from the agent
                    await self.conn.send(_answer(message))
                    continue
                event = self._notification(method_in, message.get("params"))
                if event is not None:
                    yield event
                continue
            if message.get("id") == req_id and ("result" in message or "error" in message):
                self.response = message
                return
            # A response to something we did not send: ignored.

    def _notification(self, method: str, params: Any) -> dict[str, Any] | None:
        params = params if isinstance(params, dict) else {}
        if method == "session/update":
            if not self.forward_updates:
                return None  # history replayed by session/load, or chatter before the prompt
            sid = params.get("sessionId")
            if sid is not None and str(sid).lower() != str(self.session_id).lower():
                return None
            return {**params, "type": "acp.update"}
        if not _METHOD_RE.match(method):
            return None
        return {"type": "acp.notification", "method": method, "params": params}


async def acp_turn(
    proc: asyncio.subprocess.Process,
    *,
    prompt: str,
    session_id: str | None,
    resume: bool,
    cwd: str,
    timeout_s: float,
) -> AsyncIterator[dict[str, Any]]:
    """Drive one prompt turn with the agent on ``proc``'s stdin/stdout. See the module docstring."""
    conn = _Conn(proc, time.monotonic() + timeout_s)
    ex = _Exchange(conn)
    stage = "initialize"
    try:
        try:
            init_params = {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": CLIENT_CAPABILITIES,
                "clientInfo": CLIENT_INFO,
            }
            async with contextlib.aclosing(ex.call(stage, init_params)) as events:
                async for event in events:
                    yield event
            assert ex.response is not None
            if "error" in ex.response:
                yield _rpc_error(ex.response["error"], prompt, stage)
                return
            init = ex.response.get("result") if isinstance(ex.response.get("result"), dict) else {}
            version = init.get("protocolVersion")
            if isinstance(version, int) and version != PROTOCOL_VERSION:
                yield _local_error("protocol", f"unsupported protocol version {version}", stage)
                return
            caps = init.get("agentCapabilities") if isinstance(init.get("agentCapabilities"), dict) else {}

            if resume:
                stage = "session/load"
                if not session_id:
                    yield _local_error("protocol", "resume needs a session id", stage)
                    return
                if caps.get("loadSession") is not True:
                    yield _local_error("protocol", "the agent cannot load sessions", stage)
                    return
                ex.session_id = session_id
                params: dict[str, Any] = {"sessionId": session_id, "cwd": cwd, "mcpServers": []}
            else:
                stage = "session/new"
                params = {"cwd": cwd, "mcpServers": []}
            async with contextlib.aclosing(ex.call(stage, params)) as events:
                async for event in events:
                    yield event
            assert ex.response is not None
            if "error" in ex.response:
                yield _rpc_error(ex.response["error"], prompt, stage)
                return
            created = ex.response.get("result") if isinstance(ex.response.get("result"), dict) else {}
            if not resume:
                sid = created.get("sessionId")
                if not isinstance(sid, str) or not sid:
                    yield _local_error("protocol", "the agent returned no session id", stage)
                    return
                ex.session_id = sid
            session_event: dict[str, Any] = {"type": "acp.session", "sessionId": ex.session_id}
            models = created.get("models")
            if isinstance(models, dict) and isinstance(models.get("currentModelId"), str):
                session_event["modelId"] = models["currentModelId"]
            yield session_event

            stage = "session/prompt"
            ex.forward_updates = True
            prompt_params = {"sessionId": ex.session_id, "prompt": [{"type": "text", "text": prompt}]}
            async with contextlib.aclosing(ex.call(stage, prompt_params)) as events:
                async for event in events:
                    yield event
            assert ex.response is not None
            if "error" in ex.response:
                yield _rpc_error(ex.response["error"], prompt, stage)
                return
            result = ex.response.get("result")
            yield {**(result if isinstance(result, dict) else {}), "type": "acp.result"}
        except _Closed:
            yield _local_error("eof", "the agent closed the connection before the turn finished", stage)
        except _Timeout:
            if ex.session_id is not None:
                with contextlib.suppress(_Closed, Exception):
                    await asyncio.wait_for(
                        conn.send({"method": "session/cancel", "params": {"sessionId": ex.session_id}}), 2
                    )
            yield _local_error("timeout", "the agent did not finish in time", stage)
        except _TooLarge:
            yield _local_error("output_too_large", "the agent's output exceeded the cap", stage)
    finally:
        # Closing stdin tells the agent that the client is gone; it should exit on its own.
        with contextlib.suppress(Exception):
            conn.stdin.close()
