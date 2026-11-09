"""Browser engines for the ``browser`` service.

``AgentBrowserEngine`` drives vercel-labs/agent-browser (pinned 0.41.x) against a Chrome the
service launches itself, so the service owns the network flags and a CDP guard that fails every
non-GET document request (no form can be submitted). ``FakeEngine`` keeps pages in memory for
tests and for running the service without Chrome (``ARGUS_BROWSER_ENGINE=fake``).

Browsing is read-only: only :data:`ACTIONS` exist, the CLI arguments for them are built here
from validated values, and agent-browser's ``--action-policy`` denies the write actions as well.
The live stream is watch-only: the relay sends ``config`` and ``ack`` messages, never ``input_*``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import tempfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

ACTIONS = ("open", "snapshot", "click", "scroll", "get_text", "back", "forward", "wait")
FORBIDDEN_ACTIONS = ("fill", "type", "press", "select", "upload", "download", "eval")
# agent-browser files ``press`` (and hover, focus, drag...) under its ``interact`` category.
_POLICY_DENY = (*FORBIDDEN_ACTIONS, "interact")

MAX_OUTPUT = 30_000
MAX_URL = 2000
DEFAULT_SCROLL_PX = 600
MAX_SCROLL_PX = 10_000
DEFAULT_WAIT_MS = 1000
MAX_WAIT_MS = 10_000
SCREENSHOT_QUALITY = 70
MAX_FPS = 30

DOCUMENT_PATTERNS = [{"resourceType": "Document", "requestStage": "Request"}]
SAFE_DOCUMENT_METHODS = frozenset({"GET", "HEAD"})
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
_GUARDED_TARGETS = frozenset({"page", "iframe"})

_REF = re.compile(r"^@?(e\d{1,6})$")
_TAB_ID = re.compile(r"^t(\d{1,6})$")
_DIRECTIONS = ("up", "down", "left", "right")
_BOUNDARY_START = "AGENT_BROWSER_PAGE_CONTENT"


@dataclass
class ActionResult:
    url: str
    title: str
    snapshot: str  # accessibility snapshot text, already inside content boundaries
    screenshot: bytes | None  # JPEG after the action
    error: str | None = None


class SessionGone(Exception):
    """The engine no longer has this session (the browser died, or the CDP guard dropped)."""


class Engine(Protocol):
    async def start(self, session_id: str, *, viewport: tuple[int, int]) -> None: ...
    async def act(self, session_id: str, action: str, args: dict[str, Any]) -> ActionResult: ...
    def frames(self, session_id: str, *, max_fps: int) -> AsyncIterator[bytes]: ...  # JPEG frames
    async def stop(self, session_id: str) -> None: ...


# --- pure helpers: argument building ------------------------------------------------------------


def _ref(args: dict[str, Any], *, required: bool, what: str = "click") -> str | None:
    ref = args.get("ref")
    if ref is None:
        if required:
            raise ValueError(f"{what} needs a ref from the snapshot, like @e3")
        return None
    m = _REF.match(ref) if isinstance(ref, str) else None
    if not m:
        raise ValueError("ref must be an element ref from the snapshot, like @e3")
    return "@" + m.group(1)


def _int(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{key} must be a whole number from {lo} to {hi}")
    return value


def _check_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("open needs a url")
    if len(url) > MAX_URL:
        raise ValueError(f"url is longer than {MAX_URL} characters")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("url must not contain spaces or control characters")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https URLs can be opened")
    return url


def build_command(action: str, args: dict[str, Any] | None) -> list[str]:
    """The agent-browser command for one allowed action. Raises ``ValueError`` on anything else.

    Values come from the model, so every one is checked against a closed shape: no value can
    start with ``-`` and be read as a flag, and only element refs (never CSS) are accepted.
    """
    if action not in ACTIONS:
        raise ValueError(f"action {action!r} is not allowed")
    args = {k: v for k, v in (args or {}).items() if v is not None}
    if action == "open":
        return ["open", _check_url(args.get("url"))]
    if action == "snapshot":
        return ["snapshot"] if args.get("full") is True else ["snapshot", "-i"]
    if action == "click":
        return ["click", str(_ref(args, required=True))]
    if action == "scroll":
        direction = args.get("direction", "down")
        if direction not in _DIRECTIONS:
            raise ValueError("direction must be up or down")
        key = "amount" if "amount" in args else "px"
        return ["scroll", direction, str(_int(args, key, DEFAULT_SCROLL_PX, 1, MAX_SCROLL_PX))]
    if action == "get_text":
        return ["get", "text", _ref(args, required=False, what="get_text") or "body"]
    if action == "wait":
        ref = _ref(args, required=False, what="wait")
        if ref:
            return ["wait", ref]
        return ["wait", str(_int(args, "ms", DEFAULT_WAIT_MS, 0, MAX_WAIT_MS))]
    return [action]  # back, forward


def action_policy_json() -> str:
    """agent-browser ``--action-policy`` file: everything allowed except the write actions."""
    return json.dumps({"default": "allow", "deny": list(_POLICY_DENY)})


def chrome_argv(
    chrome: str,
    *,
    port: int,
    proxy: str,
    user_data_dir: str,
    viewport: tuple[int, int],
    extra: Sequence[str] = (),
) -> list[str]:
    """Chrome launch flags. Operator ``extra`` flags come first so the confinement flags win."""
    width, height = viewport
    return [
        chrome,
        *extra,
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--proxy-server={proxy}",
        # Chrome bypasses the proxy for loopback by default; this removes that implicit rule.
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-background-networking",
        f"--user-data-dir={user_data_dir}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-component-update",
        "--disable-default-apps",
        "--mute-audio",
        "about:blank",
    ]


def agent_browser_argv(
    binary: str,
    *,
    cdp_port: int,
    session: str,
    policy_path: str,
    command: Sequence[str],
    extra_globals: Sequence[str] = (),
) -> list[str]:
    return [
        binary,
        "--cdp",
        str(cdp_port),
        "--session",
        session,
        "--json",
        "--action-policy",
        policy_path,
        "--content-boundaries",
        "--max-output",
        str(MAX_OUTPUT),
        *extra_globals,
        *command,
    ]


# --- pure helpers: parsing ----------------------------------------------------------------------


@dataclass
class CliReply:
    ok: bool
    data: Any = None
    error: str | None = None
    boundary: dict[str, Any] | None = None

    def text(self, *keys: str) -> str:
        """The string payload: ``data`` itself, or the first of ``keys`` holding a string."""
        if isinstance(self.data, str):
            return self.data
        if isinstance(self.data, dict):
            for key in keys:
                value = self.data.get(key)
                if isinstance(value, str):
                    return value
        return ""


def parse_cli_output(stdout: bytes, stderr: bytes = b"", returncode: int | None = 0) -> CliReply:
    """Parse ``agent-browser --json`` output: ``{"success", "data", "error", "_boundary"}``."""
    text = stdout.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        detail = (stderr.decode("utf-8", "replace").strip() or text)[:500]
        return CliReply(ok=False, error=detail or f"agent-browser exited with status {returncode}")
    ok = payload.get("success") is True
    boundary = payload.get("_boundary") if isinstance(payload.get("_boundary"), dict) else None
    error = None
    if not ok:
        raw = payload.get("error")
        if isinstance(raw, dict):
            raw = raw.get("message") or json.dumps(raw)
        error = str(raw)[:500] if raw else "the action failed"
    return CliReply(ok=ok, data=payload.get("data"), error=error, boundary=boundary)


def wrap_boundaries(text: str, boundary: dict[str, Any] | None, *, origin: str) -> str:
    """Put page text inside agent-browser's content boundary markers, unless it already is.

    In ``--json`` mode agent-browser reports the boundary as metadata rather than markers, so the
    markers are added here with its nonce (or a fresh one) before the text leaves the service.
    """
    if not text or _BOUNDARY_START in text:
        return text
    nonce = str((boundary or {}).get("nonce") or secrets.token_hex(8))
    origin = str((boundary or {}).get("origin") or origin)
    return (
        f"--- {_BOUNDARY_START} nonce={nonce} origin={origin} ---\n"
        f"{text}\n"
        f"--- END_{_BOUNDARY_START} nonce={nonce} ---"
    )


def parse_tab_ids(data: Any) -> list[str]:
    """Tab ids (``t<N>``) from ``agent-browser tab`` output, oldest first. Labels are ignored."""
    items = data.get("tabs") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    ids: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tab_id = item.get("tabId", item.get("id"))
        m = _TAB_ID.match(tab_id) if isinstance(tab_id, str) else None
        if m:
            ids.append((int(m.group(1)), tab_id))
    return [tab_id for _, tab_id in sorted(ids)]


def guard_commands(message: dict[str, Any]) -> list[tuple[str, dict[str, Any], str | None]]:
    """CDP commands ``(method, params, session_id)`` the guard sends in answer to one event.

    - A page (or out-of-process iframe) attaches paused: intercept its document requests, ask to
      attach its own children the same way, then let it run. Other targets just run.
    - A paused document request: ``GET``/``HEAD`` continue; any other method fails, so a form
      submission (or any POST navigation) never leaves the browser.
    """
    method = message.get("method")
    params = message.get("params") or {}
    if method == "Target.attachedToTarget":
        sid = params.get("sessionId")
        kind = (params.get("targetInfo") or {}).get("type")
        if not sid:
            return []
        if kind in _GUARDED_TARGETS:
            return [
                ("Fetch.enable", {"patterns": DOCUMENT_PATTERNS}, sid),
                ("Target.setAutoAttach", dict(_AUTO_ATTACH), sid),
                ("Runtime.runIfWaitingForDebugger", {}, sid),
            ]
        return [("Runtime.runIfWaitingForDebugger", {}, sid)]
    if method == "Fetch.requestPaused":
        sid = message.get("sessionId")
        request_id = params.get("requestId")
        verb = str((params.get("request") or {}).get("method", "")).upper()
        if verb in SAFE_DOCUMENT_METHODS:
            return [("Fetch.continueRequest", {"requestId": request_id}, sid)]
        return [("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}, sid)]
    return []


def parse_stream_message(raw: str | bytes) -> tuple[bytes, int | None] | None:
    """A JPEG ``frame`` from agent-browser's stream as ``(bytes, seq)``; anything else is ``None``."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "frame" or not isinstance(msg.get("data"), str):
        return None
    try:
        frame = base64.b64decode(msg["data"], validate=True)
    except (binascii.Error, ValueError):
        return None
    seq = msg.get("seq")
    return frame, seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def stream_config(max_fps: int) -> dict[str, Any]:
    return {"type": "config", "maxFps": max(1, min(MAX_FPS, int(max_fps))), "pacing": "ack"}


# --- websocket clients ---------------------------------------------------------------------------


async def relay_stream(url: str, max_fps: int) -> AsyncIterator[bytes]:
    """Yield JPEG frames from an agent-browser stream server, acking each one (watch-only).

    The ack is sent when the consumer asks for the next frame, so a slow viewer slows the source
    instead of queueing frames. Closing this generator closes the websocket subscription.
    """
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    async with connect(url, proxy=None, max_size=8 * 2**20, open_timeout=10) as ws:
        try:
            await ws.send(json.dumps(stream_config(max_fps)))
            async for raw in ws:
                parsed = parse_stream_message(raw)
                if parsed is None:
                    continue
                frame, seq = parsed
                yield frame
                if seq is not None:
                    await ws.send(json.dumps({"type": "ack", "seq": seq}))
        except ConnectionClosed:
            return


class CdpGuard:
    """A browser-level CDP connection that holds every page to GET/HEAD document requests."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed and self._reader is not None and not self._reader.done()

    async def start(self) -> None:
        from websockets.asyncio.client import connect

        # CDP events (a paused POST carries its body) can be large; Chrome is on loopback.
        self._ws = await connect(self.ws_url, proxy=None, max_size=64 * 2**20, open_timeout=10)
        self._reader = asyncio.create_task(self._read())
        await asyncio.wait_for(self.call("Target.setAutoAttach", dict(_AUTO_ATTACH)), 10)

    async def call(self, method: str, params: dict[str, Any], session_id: str | None = None) -> Any:
        if self._ws is None or not self.alive:
            raise ConnectionError("CDP guard is not connected")
        self._next_id += 1
        msg: dict[str, Any] = {"id": self._next_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._next_id] = fut
        try:
            await self._ws.send(json.dumps(msg))
            return await fut
        finally:
            self._pending.pop(msg["id"], None)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result"))
                    continue
                commands = guard_commands(msg)
                if commands:
                    task = asyncio.create_task(self._send_in_order(commands))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        except Exception as exc:  # the connection dropped: the session is no longer guarded
            log.warning("browser CDP guard disconnected: %s", exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP guard disconnected"))

    async def _send_in_order(self, commands: list[tuple[str, dict[str, Any], str | None]]) -> None:
        # Await each reply: interception must be on before the paused page is allowed to run.
        for method, params, session_id in commands:
            try:
                await asyncio.wait_for(self.call(method, params, session_id), 10)
            except Exception as exc:  # the target went away, or Chrome refused: nothing to guard
                log.debug("CDP guard %s failed: %s", method, exc)
                return

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader


# --- the agent-browser engine --------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class _AbSession:
    session_id: str
    workdir: Path
    cdp_port: int
    chrome: asyncio.subprocess.Process
    guard: CdpGuard
    stream_port: int | None = None
    shots: int = 0
    stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentBrowserEngine:
    """One headless Chrome per session, driven by the agent-browser CLI attached over CDP."""

    def __init__(
        self,
        *,
        binary: str = "agent-browser",
        chrome: str = "chrome",
        proxy: str = "http://egress:3128",
        action_timeout_s: float = 25.0,
        work_root: str | None = None,
        chrome_extra_args: Sequence[str] = (),
        start_timeout_s: float = 20.0,
    ) -> None:
        self.binary = binary
        self.chrome = chrome
        self.proxy = proxy
        self.action_timeout_s = action_timeout_s
        self.work_root = Path(work_root or tempfile.gettempdir())
        self.chrome_extra_args = tuple(chrome_extra_args)
        self.start_timeout_s = start_timeout_s
        self._sessions: dict[str, _AbSession] = {}
        self._policy_path: Path | None = None

    @classmethod
    def from_env(cls) -> AgentBrowserEngine:
        env = os.environ
        return cls(
            binary=env.get("ARGUS_BROWSER_AGENT_BIN", "agent-browser"),
            chrome=env.get("ARGUS_BROWSER_CHROME_BIN", "chrome"),
            proxy=env.get("ARGUS_BROWSER_PROXY", "http://egress:3128"),
            action_timeout_s=float(env.get("ARGUS_BROWSER_ACTION_TIMEOUT_S", "25")),
            work_root=env.get("ARGUS_BROWSER_WORKDIR") or None,
            chrome_extra_args=shlex.split(env.get("ARGUS_BROWSER_CHROME_EXTRA_ARGS", "")),
        )

    def _policy(self) -> str:
        if self._policy_path is None or not self._policy_path.exists():
            self.work_root.mkdir(parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="argus-browser-policy-", suffix=".json", dir=self.work_root)
            with os.fdopen(fd, "w") as fh:
                fh.write(action_policy_json())
            self._policy_path = Path(path)
        return str(self._policy_path)

    def _session(self, session_id: str) -> _AbSession:
        s = self._sessions.get(session_id)
        if s is None or s.chrome.returncode is not None or not s.guard.alive:
            raise SessionGone(session_id)
        return s

    # -- lifecycle --

    async def start(self, session_id: str, *, viewport: tuple[int, int]) -> None:
        if not self.proxy:
            raise RuntimeError("ARGUS_BROWSER_PROXY is not set; refusing to start an unconfined browser")
        policy = self._policy()
        self.work_root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"argus-browser-{session_id}-", dir=self.work_root))
        port = _free_port()
        argv = chrome_argv(
            self.chrome,
            port=port,
            proxy=self.proxy,
            user_data_dir=str(workdir / "profile"),
            viewport=viewport,
            extra=self.chrome_extra_args,
        )
        chrome = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # its own process group, so stop() reaps the helpers too
        )
        guard: CdpGuard | None = None
        try:
            ws_url = await self._wait_for_cdp(chrome, port)
            guard = CdpGuard(ws_url)
            await guard.start()
        except BaseException:
            if guard is not None:
                await guard.close()
            await _kill_group(chrome)
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        self._sessions[session_id] = _AbSession(session_id, workdir, port, chrome, guard)
        log.info("browser session %s started (cdp %s, policy %s)", session_id, port, policy)

    async def _wait_for_cdp(self, chrome: asyncio.subprocess.Process, port: int) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.start_timeout_s
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while True:
                if chrome.returncode is not None:
                    raise RuntimeError(f"Chrome exited with status {chrome.returncode} on start")
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/json/version")
                    url = r.json().get("webSocketDebuggerUrl")
                    if r.status_code == 200 and isinstance(url, str):
                        return url
                except (httpx.HTTPError, ValueError):
                    pass
                if loop.time() > deadline:
                    raise RuntimeError("Chrome did not open its debugging port in time")
                await asyncio.sleep(0.1)

    async def stop(self, session_id: str) -> None:
        s = self._sessions.pop(session_id, None)
        if s is None:
            return
        with contextlib.suppress(Exception):
            await self._cli(s, ["close"], limit_s=5)
        await s.guard.close()
        await _kill_group(s.chrome)
        shutil.rmtree(s.workdir, ignore_errors=True)

    # -- actions --

    async def _cli(
        self, s: _AbSession, command: Sequence[str], *, limit_s: float, extra_globals: Sequence[str] = ()
    ) -> CliReply:
        argv = agent_browser_argv(
            self.binary,
            cdp_port=s.cdp_port,
            session=s.session_id,
            policy_path=self._policy(),
            command=command,
            extra_globals=extra_globals,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CliReply(ok=False, error=f"agent-browser could not run: {exc.strerror or exc}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), limit_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return CliReply(ok=False, error=f"timed out after {limit_s:.0f}s")
        return parse_cli_output(out, err, proc.returncode)

    async def act(self, session_id: str, action: str, args: dict[str, Any]) -> ActionResult:
        s = self._session(session_id)
        command = build_command(action, args)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.action_timeout_s

        def left(share: float = 1.0) -> float:
            return max(0.0, (deadline - loop.time()) * share)

        reply = await self._cli(s, command, limit_s=self.action_timeout_s * 0.6)
        error = reply.error
        if error and error.startswith("timed out"):
            error = (
                f"{action} {error}: the page did not finish loading. "
                "Try snapshot or wait to read what has loaded, or back."
            )
        self._session(session_id)  # Chrome or the guard died during the action: fail closed

        if action == "click" and left() > 1:
            await self._follow_newest_tab(s, left)

        content, boundary = "", None
        if action in ("snapshot", "get_text"):
            if reply.ok:
                content, boundary = reply.text("snapshot", "text", "content", "result"), reply.boundary
        elif left() > 1:
            snap = await self._cli(s, ["snapshot", "-i"], limit_s=left(0.5))
            content, boundary = snap.text("snapshot", "content"), snap.boundary

        url = title = ""
        if left() > 0.5:
            url = (await self._cli(s, ["get", "url"], limit_s=left(0.5))).text("url")
        if left() > 0.5:
            title = (await self._cli(s, ["get", "title"], limit_s=left(0.5))).text("title")
        screenshot = await self._screenshot(s, left()) if left() > 0.5 else None
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" if url else ""
        return ActionResult(
            url=url,
            title=title,
            snapshot=wrap_boundaries(content, boundary, origin=origin),
            screenshot=screenshot,
            error=error,
        )

    async def _follow_newest_tab(self, s: _AbSession, left: Any) -> None:
        """A click that opened a tab: switch to the newest one and close the rest."""
        tabs = parse_tab_ids((await self._cli(s, ["tab"], limit_s=min(5.0, left()))).data)
        if len(tabs) < 2:
            return
        newest = tabs[-1]
        if not (await self._cli(s, ["tab", newest], limit_s=min(5.0, left()))).ok:
            return
        for tab in tabs[:-1]:
            await self._cli(s, ["tab", "close", tab], limit_s=min(5.0, left()))

    async def _screenshot(self, s: _AbSession, limit_s: float) -> bytes | None:
        s.shots += 1
        path = s.workdir / f"shot-{s.shots}.jpg"
        reply = await self._cli(
            s,
            ["screenshot", str(path)],
            limit_s=limit_s,
            extra_globals=["--screenshot-format", "jpeg", "--screenshot-quality", str(SCREENSHOT_QUALITY)],
        )
        try:
            if path.exists():
                return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        if reply.ok and isinstance(reply.data, dict) and isinstance(reply.data.get("base64"), str):
            with contextlib.suppress(binascii.Error, ValueError):
                return base64.b64decode(reply.data["base64"], validate=True)
        return None

    # -- live frames --

    async def _stream_port(self, s: _AbSession) -> int:
        async with s.stream_lock:
            if s.stream_port is None:
                port = _free_port()
                reply = await self._cli(s, ["stream", "enable", "--port", str(port)], limit_s=10)
                if not reply.ok:
                    raise RuntimeError(f"cannot start the live view: {reply.error}")
                bound = reply.data.get("port") if isinstance(reply.data, dict) else None
                s.stream_port = bound if isinstance(bound, int) else port
            return s.stream_port

    async def frames(self, session_id: str, *, max_fps: int) -> AsyncIterator[bytes]:
        s = self._session(session_id)
        port = await self._stream_port(s)
        async for frame in relay_stream(f"ws://127.0.0.1:{port}", max_fps):
            yield frame

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.stop(session_id)


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    # Helpers (renderer, GPU) can outlive the main process; the group is gone with them.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


# --- the fake engine ------------------------------------------------------------------------------


@dataclass
class _FakePage:
    history: list[str] = field(default_factory=lambda: ["about:blank"])
    index: int = 0

    @property
    def url(self) -> str:
        return self.history[self.index]


class FakeEngine:
    """In-memory pages for tests and Chrome-less runs. Records every call."""

    SCREENSHOT = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

    def __init__(
        self,
        frames: Sequence[bytes] = (),
        *,
        endless: bool = False,
        frame_interval_s: float = 0.0,
        act_delay_s: float = 0.0,
    ) -> None:
        self.sessions: dict[str, _FakePage] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.frame_requests: list[tuple[str, int]] = []
        self.streams_open = 0
        self.streams_closed = 0
        self._frames = list(frames)
        self._endless = endless
        self._frame_interval_s = frame_interval_s
        self._act_delay_s = act_delay_s

    async def start(self, session_id: str, *, viewport: tuple[int, int]) -> None:
        self.started.append(session_id)
        self.sessions[session_id] = _FakePage()

    async def act(self, session_id: str, action: str, args: dict[str, Any]) -> ActionResult:
        page = self.sessions.get(session_id)
        if page is None:
            raise SessionGone(session_id)
        self.calls.append((session_id, action, dict(args)))
        if self._act_delay_s:
            await asyncio.sleep(self._act_delay_s)
        if action == "open":
            del page.history[page.index + 1 :]
            page.history.append(args["url"])
            page.index += 1
        elif action == "back":
            page.index = max(0, page.index - 1)
        elif action == "forward":
            page.index = min(len(page.history) - 1, page.index + 1)
        title = f"Page {page.url}"
        snapshot = wrap_boundaries(f'- heading "{title}" [ref=e1]', {"nonce": "fake"}, origin=page.url)
        return ActionResult(url=page.url, title=title, snapshot=snapshot, screenshot=self.SCREENSHOT)

    async def frames(self, session_id: str, *, max_fps: int) -> AsyncIterator[bytes]:
        if session_id not in self.sessions:
            raise SessionGone(session_id)
        self.frame_requests.append((session_id, max_fps))
        self.streams_open += 1
        try:
            if self._endless:
                n = 0
                while session_id in self.sessions:
                    n += 1
                    yield b"\xff\xd8frame-%d" % n
                    await asyncio.sleep(self._frame_interval_s)
            else:
                for frame in self._frames:
                    yield frame
                    if self._frame_interval_s:
                        await asyncio.sleep(self._frame_interval_s)
        finally:
            self.streams_closed += 1

    async def stop(self, session_id: str) -> None:
        if self.sessions.pop(session_id, None) is not None:
            self.stopped.append(session_id)

    async def close_all(self) -> None:
        for session_id in list(self.sessions):
            await self.stop(session_id)
