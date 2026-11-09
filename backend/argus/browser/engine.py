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
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from argus.browser.limits import (
    DEFAULT_ACTION_TIMEOUT_S,
    START_TIMEOUT_S,
    action_budget,
    action_timeout_from_env,
    check_action_timeout,
    command_limit,
)

log = logging.getLogger(__name__)

ACTIONS = ("open", "snapshot", "click", "scroll", "get_text", "back", "forward", "wait")
FORBIDDEN_ACTIONS = ("fill", "type", "press", "select", "upload", "download", "eval")
# agent-browser's ``--action-policy`` matches the daemon's wire action names (agent-browser 0.41.2,
# cli/src/commands.rs), not the CLI words: ``open`` is "navigate", ``get text`` "gettext", ``eval``
# "evaluate". Default deny: only what this engine sends is allowed ("launch" is sent by the CLI
# itself when it attaches with ``--cdp``), and the write actions are named in ``deny`` as well.
_POLICY_ALLOW = (
    "launch",
    "close",
    "navigate",
    "snapshot",
    "click",
    "scroll",
    "gettext",
    "url",
    "title",
    "back",
    "forward",
    "wait",
    "tab_list",
    "tab_switch",
    "tab_close",
    "screenshot",
    "stream_enable",
    "stream_status",
)
_POLICY_DENY = (
    *FORBIDDEN_ACTIONS,
    "evaluate",
    "keyboard",
    "keydown",
    "keyup",
    "check",
    "uncheck",
    "waitforfunction",
    "waitfordownload",
)

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
# A service worker can answer a navigation itself (and pass a form POST through); pages bypass
# service workers, and the workers' own document requests are held to GET/HEAD as well.
_GUARDED_WORKERS = frozenset({"service_worker", "shared_worker"})
# The guard ignores Network events and never reads bodies: keep Chrome's per-session buffers small.
_NETWORK_ENABLE = {"maxTotalBufferSize": 1_000_000, "maxResourceBufferSize": 100_000}
# The only Chrome flags an operator may add (the container needs some of them). Anything else
# could take Chrome off the proxy or loosen the debugging port, so it is refused at startup.
ALLOWED_CHROME_EXTRA_ARGS = frozenset(
    {"--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-software-rasterizer"}
)
# Chrome's own background traffic (Google services, not the page) despite
# --disable-background-networking: the search preconnect to www.google.com, network time
# (clients2.google.com), autofill field types (content-autofill.googleapis.com), optimization
# hints, translate, Cast discovery, and so on. Names from Playwright's chromiumSwitches.ts and
# Puppeteer's ChromeLauncher defaults. The managed policy baked into the image
# (docker/chrome-policy.json) turns off the rest, e.g. captive portal checks (connectivitycheck
# .gstatic.com), which have no switch.
_DISABLED_FEATURES = (
    "AutofillServerCommunication",
    "OptimizationHints",
    "MediaRouter",
    "Translate",
    "NetworkTimeServiceQuerying",
    "PreconnectToSearch",
    "AimEnabled",
    "LensOverlay",
    "CertificateTransparencyComponentUpdater",
)
QUIET_CHROME_ARGS = (
    f"--disable-features={','.join(_DISABLED_FEATURES)}",
    "--enable-features=SkipListAccountsWithoutGaiaCookies",
    "--disable-field-trial-config",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--safebrowsing-disable-auto-update",
    "--disable-component-extensions-with-background-pages",
    "--disable-search-engine-choice-screen",
    "--metrics-recording-only",
    "--no-pings",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--no-service-autorun",
)
# What Chrome and agent-browser inherit: never the service token or the rest of our environment.
# An exact list, with no ``AGENT_BROWSER_*`` passthrough: operator env cannot pin one stream port
# for every session or put another action policy ahead of the service's own flags.
_CHILD_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "AGENT_BROWSER_SOCKET_DIR")

_REF = re.compile(r"^@?(e\d{1,6})$")
_TAB_ID = re.compile(r"^t(\d{1,6})$")
_DIRECTIONS = ("up", "down", "left", "right")
_BOUNDARY_START = "AGENT_BROWSER_PAGE_CONTENT"
# Page text one action returns, before the boundary markers are added (they always fit after it).
MAX_PAGE_CHARS = 30_000
TRUNCATED_NOTE = "[truncated]"


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
    """agent-browser ``--action-policy`` file: only the engine's own actions, never a write action."""
    return json.dumps({"default": "deny", "allow": list(_POLICY_ALLOW), "deny": list(_POLICY_DENY)})


def check_chrome_extra_args(extra: Sequence[str]) -> tuple[str, ...]:
    """Operator Chrome flags, each one exactly from :data:`ALLOWED_CHROME_EXTRA_ARGS`."""
    refused = [arg for arg in extra if arg not in ALLOWED_CHROME_EXTRA_ARGS]
    if refused:
        raise ValueError(
            "ARGUS_BROWSER_CHROME_EXTRA_ARGS may only contain "
            f"{', '.join(sorted(ALLOWED_CHROME_EXTRA_ARGS))}; refusing {', '.join(refused)}"
        )
    return tuple(extra)


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for Chrome and agent-browser (no service token, no API settings)."""
    environ = dict(os.environ if environ is None else environ)
    env = {k: environ[k] for k in _CHILD_ENV_KEYS if k in environ}
    # agent-browser would otherwise fetch chromereleases.googleblog.com itself, outside the proxy.
    env["AGENT_BROWSER_NO_VERSION_CHECK"] = "1"
    return env


def chrome_argv(
    chrome: str,
    *,
    proxy: str,
    user_data_dir: str,
    viewport: tuple[int, int],
    extra: Sequence[str] = (),
) -> list[str]:
    """Chrome launch flags. Only allowlisted operator flags are accepted.

    The debugging port is ``0``: Chrome picks a free one and writes it to ``DevToolsActivePort``
    in this session's own profile, so the service never attaches to another session's browser.
    """
    width, height = viewport
    return [
        chrome,
        *check_chrome_extra_args(extra),
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        f"--proxy-server={proxy}",
        # Chrome bypasses the proxy for loopback by default; this removes that implicit rule.
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-background-networking",
        *QUIET_CHROME_ARGS,
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
    timed_out: bool = False  # the service stopped waiting for agent-browser

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


def timeout_message(action: str, limit_s: float) -> str:
    """The error an agent sees when an action's command did not return in time.

    agent-browser waits for a page's load event; the page itself has often loaded by then (or
    loaded enough to read), so the agent is pointed at reading it rather than told it failed.
    """
    return (
        f"{action} did not return within {limit_s:.0f}s (the browser was still waiting, for example "
        "for the page's load event). The page may have loaded anyway: use snapshot or get_text to "
        "read what is there, or back."
    )


@dataclass
class ProcessRun:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    elapsed_s: float


async def collect_process(proc: asyncio.subprocess.Process, limit_s: float) -> ProcessRun:
    """Wait up to ``limit_s`` for ``proc`` (stdout and stderr piped), keeping whatever it wrote.

    It returns as soon as stdout holds a complete JSON answer, without waiting for the pipes to
    close: agent-browser's first command in a session starts its daemon, which inherits stdout and
    stderr and keeps them open for its whole life. A CLI still running a moment after its answer
    is ended. On a timeout the process is killed, and what it had written so far is returned with
    it, so a hang can be explained from its own output.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    out, err = bytearray(), bytearray()
    answered = asyncio.Event()

    async def drain(stream: asyncio.StreamReader | None, into: bytearray, watch: bool) -> None:
        if stream is None:
            return
        while chunk := await stream.read(65536):
            into += chunk
            if watch and _complete_json(into):
                answered.set()

    readers = asyncio.gather(drain(proc.stdout, out, True), drain(proc.stderr, err, False), proc.wait())
    answer = asyncio.ensure_future(answered.wait())
    timed_out = False
    try:
        await asyncio.wait({readers, answer}, timeout=limit_s, return_when=asyncio.FIRST_COMPLETED)
        if not readers.done():
            if answer.done():
                # Answered: give the CLI a moment to exit by itself, then stop listening.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(proc.wait()), ANSWER_EXIT_GRACE_S)
            else:
                timed_out = True
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            # The pipes may stay open in a daemon the CLI started: never wait long for them.
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(readers), 0.2)
            if not readers.done():
                readers.cancel()
                with contextlib.suppress(BaseException):
                    await readers
            await proc.wait()
    finally:
        answer.cancel()
    return ProcessRun(bytes(out), bytes(err), proc.returncode, timed_out, loop.time() - started)


# How long a CLI may keep running after it has printed its whole answer.
ANSWER_EXIT_GRACE_S = 0.5


def _complete_json(data: bytearray) -> bool:
    """Whether ``data`` already holds one whole JSON document (agent-browser's ``--json`` answer)."""
    text = bytes(data).strip()
    if not text.endswith((b"}", b"]")):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _guard_stuck(guard: Any) -> list[str]:
    stuck = getattr(guard, "stuck", None)
    return stuck() if callable(stuck) else []


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


def _strip_cli_markers(text: str, nonce: str | None) -> str:
    """Remove agent-browser's own marker pair, only when it is exactly the first and last line
    and carries the nonce the CLI reported (in its metadata, which the page cannot write)."""
    if not nonce:
        return text
    lines = text.split("\n")
    if len(lines) < 2:
        return text
    first, last = lines[0].strip(), lines[-1].strip()
    if (
        first.startswith(f"--- {_BOUNDARY_START} nonce={nonce} ")
        and first.endswith(" ---")
        and last == f"--- END_{_BOUNDARY_START} nonce={nonce} ---"
    ):
        return "\n".join(lines[1:-1])
    return text


def _cap(text: str, max_chars: int | None) -> str:
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars)] + "\n" + TRUNCATED_NOTE


def wrap_boundaries(
    text: str, boundary: dict[str, Any] | None, *, origin: str, max_chars: int | None = None
) -> str:
    """Put page text inside content boundary markers carrying a fresh, service-owned nonce.

    Always wraps, whatever the text contains: page text that imitates a marker (or forges an END
    line) stays inside, because only the service knows the nonce of the real END line. Text over
    ``max_chars`` is cut *before* wrapping, with a truncation note inside the boundary, so the END
    line is never what gets cut off.
    """
    if not text:
        return text
    cli_nonce = (boundary or {}).get("nonce")
    text = _strip_cli_markers(text, cli_nonce if isinstance(cli_nonce, str) else None)
    text = _cap(text, max_chars)
    nonce = secrets.token_hex(16)
    origin = re.sub(r"\s+", "", origin or str((boundary or {}).get("origin") or ""))[:200]
    return (
        f"--- {_BOUNDARY_START} nonce={nonce} origin={origin} ---\n"
        f"{text}\n"
        f"--- END_{_BOUNDARY_START} nonce={nonce} ---"
    )


_START_LINE = re.compile(rf"^--- {_BOUNDARY_START} nonce=([0-9a-f]+) origin=\S* ---$")


def split_boundaries(text: str) -> tuple[str, str, str] | None:
    """``(start line, page text, END line)`` of text wrapped by :func:`wrap_boundaries`, or None.

    Only the service's own pair counts: the first and last lines, with the same nonce.
    """
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    m = _START_LINE.match(lines[0])
    if not m or lines[-1] != f"--- END_{_BOUNDARY_START} nonce={m.group(1)} ---":
        return None
    return lines[0], "\n".join(lines[1:-1]), lines[-1]


def unwrap_boundaries(text: str) -> str:
    """The page text inside the service's boundary markers (the text itself when it is not wrapped)."""
    parts = split_boundaries(text)
    return parts[1] if parts else text


def cap_boundaries(text: str, max_chars: int) -> str:
    """Wrapped text cut to about ``max_chars``, inside the boundary: the END line always survives."""
    if len(text) <= max_chars:
        return text
    parts = split_boundaries(text)
    if parts is None:
        return _cap(text, max_chars)
    start, inner, end = parts
    room = max_chars - len(start) - len(end) - len(TRUNCATED_NOTE) - 3
    return f"{start}\n{_cap(inner, room)}\n{end}"


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


# The full GUID form only: a torn read of DevToolsActivePort (a truncated GUID) is retried.
_DEVTOOLS_PATH = re.compile(
    r"^/devtools/browser/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def parse_devtools_active_port(text: str) -> tuple[int, str] | None:
    """``(port, browser websocket path)`` from Chrome's ``DevToolsActivePort`` file, if complete."""
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].strip().isdigit():
        return None
    port, path = int(lines[0].strip()), lines[1].strip()
    if not 0 < port < 65536 or not _DEVTOOLS_PATH.match(path):
        return None
    return port, path


# Guard steps by name: only the diagnostic tool (argus.browser.diagnose) ever leaves any out, to
# find which one a hang depends on. The service's guard always sends every step.
GUARD_STEPS = {
    "fetch": "Fetch.enable",
    "network": "Network.enable",
    "bypass": "Network.setBypassServiceWorker",
    "autoattach": "Target.setAutoAttach",
}


def guard_commands(
    message: dict[str, Any], skip: frozenset[str] = frozenset()
) -> list[tuple[str, dict[str, Any], str | None]]:
    """CDP commands ``(method, params, session_id)`` the guard sends in answer to one event.

    - A page (or out-of-process iframe) attaches paused: intercept its document requests, turn the
      Network domain on and bypass service workers (a worker answering a navigation would skip
      the page's interception; the bypass needs Network enabled), ask to attach its own children
      the same way, then let it run.
    - A service or shared worker attaches paused: intercept its document requests, then run.
      Other targets just run.
    - A paused document request: ``GET``/``HEAD`` continue; any other method fails, so a form
      submission (or any POST navigation) never leaves the browser.

    If any command fails the sequence stops and the target stays paused: fail closed (unless the
    target is already gone, see :class:`CdpGuard`). ``skip`` is for the diagnostic tool only.
    """
    method = message.get("method")
    params = message.get("params") or {}
    if method == "Target.attachedToTarget":
        sid = params.get("sessionId")
        kind = (params.get("targetInfo") or {}).get("type")
        if not sid:
            return []
        if kind in _GUARDED_TARGETS:
            commands = [
                ("Fetch.enable", {"patterns": DOCUMENT_PATTERNS}, sid),
                ("Network.enable", dict(_NETWORK_ENABLE), sid),
                ("Network.setBypassServiceWorker", {"bypass": True}, sid),
                ("Target.setAutoAttach", dict(_AUTO_ATTACH), sid),
                ("Runtime.runIfWaitingForDebugger", {}, sid),
            ]
        elif kind in _GUARDED_WORKERS:
            commands = [
                ("Fetch.enable", {"patterns": DOCUMENT_PATTERNS}, sid),
                ("Runtime.runIfWaitingForDebugger", {}, sid),
            ]
        else:
            commands = [("Runtime.runIfWaitingForDebugger", {}, sid)]
        skipped = {GUARD_STEPS[s] for s in skip if s in GUARD_STEPS}
        return [c for c in commands if c[0] not in skipped]
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


def stream_port_from(data: Any) -> int | None:
    """The bound port from ``agent-browser stream status --json`` data, when streaming is on."""
    if not isinstance(data, dict) or data.get("enabled") is not True:
        return None
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    return port


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


# Chrome's answers when a command's session or target no longer exists (code -32001, or the
# target closed under it). Nothing is left to guard then, so this is not a reason to fail closed.
_GONE_ERRORS = (
    "session with given id not found",
    "no session with given id",
    "no target with given id",
    "target closed",
    "target crashed",
)
GUARD_STEP_TIMEOUT_S = 10.0
# How long after the resume the pipelined commands' answers may take (the renderer starts first).
GUARD_ANSWER_TIMEOUT_S = 20.0
# Answered by the browser process even for a target paused at start: awaited before the resume.
_AWAITED_BEFORE_RESUME = frozenset({"Fetch.enable"})
# Sent before the resume without waiting (they may need the target's renderer to answer), and
# confirmed after it: a refusal of any of them ends the session.
_CONFIRMED_AFTER_RESUME = frozenset(
    {"Network.enable", "Network.setBypassServiceWorker", "Target.setAutoAttach"}
)


class CdpError(RuntimeError):
    """Chrome answered a CDP command with an error."""

    def __init__(self, error: Any) -> None:
        self.code = error.get("code") if isinstance(error, dict) else None
        self.text = str(error.get("message", error)) if isinstance(error, dict) else str(error)
        super().__init__(str(error))

    @property
    def target_gone(self) -> bool:
        return self.code == -32001 or any(g in self.text.lower() for g in _GONE_ERRORS)


@dataclass
class GuardTarget:
    """What the guard knows about one attached target (for logs and the diagnostic tool)."""

    kind: str
    url: str
    waiting: bool
    attached_at: float
    state: str = "guarding"  # guarding -> running | failed | gone
    step: str = ""
    error: str = ""

    def describe(self, session_id: str, now: float) -> str:
        at = f" at {self.step}" if self.step else ""
        why = f": {self.error}" if self.error else ""
        return (
            f"{self.kind} {self.url[:120] or '-'} (session {session_id[:8]}, {self.state}{at}{why}, "
            f"{now - self.attached_at:.1f}s since attach)"
        )


class CdpGuard:
    """A browser-level CDP connection that holds every page to GET/HEAD document requests.

    ``on_lost`` runs (once) the moment the connection drops without ``close()``: the engine
    kills that Chrome then, so no page is ever left running unguarded.

    Each attached target is tracked (:meth:`stuck`): a target the guard could not prepare stays
    paused (fail closed), and the engine names it in its log when an action then times out. A
    target that is already gone when a command reaches it is not a failure: nothing is left to
    guard. At DEBUG every attach and detach, command and answer, and paused request is logged.

    ``skip`` leaves guard steps out (see :data:`GUARD_STEPS`); only the diagnostic tool passes it.
    """

    def __init__(
        self,
        ws_url: str,
        on_lost: Callable[[], None] | None = None,
        *,
        skip: frozenset[str] = frozenset(),
    ) -> None:
        self.ws_url = ws_url
        self.on_lost = on_lost
        self.skip = skip
        self.targets: dict[str, GuardTarget] = {}
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed and self._reader is not None and not self._reader.done()

    def stuck(self, older_than_s: float = 0.0) -> list[str]:
        """Targets the guard has not let run (still being prepared, or failed and left paused)."""
        now = time.monotonic()
        return [
            t.describe(sid, now)
            for sid, t in self.targets.items()
            if t.state in ("guarding", "failed") and now - t.attached_at >= older_than_s
        ]

    def _note_event(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            sid = str(params.get("sessionId") or "")
            target = GuardTarget(
                kind=str(info.get("type", "?")),
                url=str(info.get("url", "")),
                waiting=params.get("waitingForDebugger") is True,
                attached_at=time.monotonic(),
            )
            if sid:
                self.targets[sid] = target
            log.debug(
                "CDP guard: attached %s %s session=%s parent=%s waitingForDebugger=%s",
                target.kind,
                target.url[:200],
                sid,
                msg.get("sessionId"),
                target.waiting,
            )
        elif method == "Target.detachedFromTarget":
            sid = str(params.get("sessionId") or "")
            gone = self.targets.pop(sid, None)
            log.debug(
                "CDP guard: detached session=%s (%s, was %s)",
                sid,
                gone.kind if gone else "unknown",
                gone.state if gone else "-",
            )
        elif method == "Fetch.requestPaused":
            request = params.get("request") or {}
            log.debug(
                "CDP guard: paused %s %s %s session=%s",
                params.get("resourceType"),
                request.get("method"),
                str(request.get("url", ""))[:200],
                msg.get("sessionId"),
            )

    async def start(self) -> None:
        from websockets.asyncio.client import connect

        # CDP events (a paused POST carries its body) can be large; Chrome is on loopback.
        self._ws = await connect(self.ws_url, proxy=None, max_size=64 * 2**20, open_timeout=10)
        self._reader = asyncio.create_task(self._read())
        await asyncio.wait_for(self.call("Browser.setDownloadBehavior", {"behavior": "deny"}), 10)
        await asyncio.wait_for(self.call("Target.setAutoAttach", dict(_AUTO_ATTACH)), 10)

    async def _send(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> asyncio.Future:
        """Send one command; the returned future gets Chrome's answer (or a :class:`CdpError`)."""
        if self._ws is None or not self.alive:
            raise ConnectionError("CDP guard is not connected")
        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        fut.add_done_callback(lambda _f: self._pending.pop(msg_id, None))
        try:
            await self._ws.send(json.dumps(msg))
        except BaseException:
            fut.cancel()
            raise
        return fut

    async def call(self, method: str, params: dict[str, Any], session_id: str | None = None) -> Any:
        return await (await self._send(method, params, session_id))

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
                            fut.set_exception(CdpError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result"))
                    continue
                self._note_event(msg)
                commands = guard_commands(msg, self.skip)
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
            if not self._closed and self.on_lost is not None:
                self._closed = True
                try:
                    self.on_lost()
                except Exception:
                    log.exception("browser CDP guard: on_lost failed")

    async def _send_in_order(self, commands: list[tuple[str, dict[str, Any], str | None]]) -> None:
        """Prepare an attached target (then let it run), or answer a paused request.

        A target paused at start (``waitForDebuggerOnStart``) has no running renderer, so Chrome
        cannot answer the commands its renderer handles (``Network.enable`` among them) until it
        runs: awaiting those before the resume deadlocks until the guard gives up, and the target
        (with every page that waits on it) never runs. So only the commands Chrome answers in the
        browser process, :data:`_AWAITED_BEFORE_RESUME` (document interception), are awaited before
        the resume. The rest are sent in order right behind them, then the resume: Chrome
        dispatches one session's commands in order and applies their browser-side part (Network
        on, the service-worker bypass, auto-attach) as it does, so all of it is in place before
        the target runs. Their answers are checked after the resume, and if Chrome refused any of
        them the guarantee is not there for a running target: the whole session ends.
        """
        if commands[-1][0] != "Runtime.runIfWaitingForDebugger":  # a paused request's answer
            for method, params, session_id in commands:
                await self._await_step(None, method, params, session_id, "request left paused")
            return
        session_id = commands[0][2]
        target = self.targets.get(session_id or "")
        for method, params, sid in commands:
            if method in _AWAITED_BEFORE_RESUME and not await self._await_step(target, method, params, sid):
                return  # fail closed: nothing else is sent, the target stays paused
        loop = asyncio.get_running_loop()
        sent: list[tuple[str, str | None, asyncio.Future, float]] = []
        for method, params, sid in commands:
            if method in _AWAITED_BEFORE_RESUME:
                continue
            log.debug("CDP guard: -> %s session=%s %s (pipelined)", method, sid, json.dumps(params)[:200])
            try:
                sent.append((method, sid, await self._send(method, params, sid), loop.time()))
            except Exception as exc:  # the guard's own connection is going away: on_lost handles it
                self._failed(target, method, sid, str(exc) or type(exc).__name__)
                return
        if target is not None:
            target.step = "answers after resume"
        deadline = loop.time() + GUARD_ANSWER_TIMEOUT_S
        for method, sid, fut, started in sent:
            try:
                result = await asyncio.wait_for(fut, max(0.01, deadline - loop.time()))
            except CdpError as exc:
                if exc.target_gone:
                    self._gone(target, method, sid, exc.text)
                    return
                problem = f"Chrome refused it: {exc.text}"
            except TimeoutError:
                if (sid or "") not in self.targets:  # detached meanwhile (a closed popup, a frame)
                    self._gone(target, method, sid, "detached")
                    return
                problem = f"no answer within {GUARD_ANSWER_TIMEOUT_S:.0f}s of the resume"
            except Exception as exc:  # the guard's own connection is going away
                problem = str(exc) or type(exc).__name__
            else:
                log.debug(
                    "CDP guard: <- %s session=%s ok in %.0fms %s",
                    method,
                    sid,
                    (loop.time() - started) * 1000,
                    json.dumps(result)[:200] if result else "",
                )
                if target is not None and method == "Runtime.runIfWaitingForDebugger":
                    target.state, target.step = "running", ""
                continue
            if method in _CONFIRMED_AFTER_RESUME:
                self._end_session(target, method, sid, problem)
            else:
                self._failed(target, method, sid, problem)
            return

    async def _await_step(
        self,
        target: GuardTarget | None,
        method: str,
        params: dict[str, Any],
        session_id: str | None,
        fail_closed_note: str = "",
    ) -> bool:
        """Send one command and wait for its answer. False when it failed (or the target is gone)."""
        if target is not None:
            target.step = method
        loop = asyncio.get_running_loop()
        started = loop.time()
        log.debug("CDP guard: -> %s session=%s %s", method, session_id, json.dumps(params)[:200])
        try:
            result = await asyncio.wait_for(self.call(method, params, session_id), GUARD_STEP_TIMEOUT_S)
        except CdpError as exc:
            if exc.target_gone:
                self._gone(target, method, session_id, exc.text)
                return False
            self._failed(target, method, session_id, f"Chrome refused it: {exc.text}", fail_closed_note)
            return False
        except TimeoutError:
            problem = f"no answer in {GUARD_STEP_TIMEOUT_S:.0f}s"
            self._failed(target, method, session_id, problem, fail_closed_note)
            return False
        except Exception as exc:  # the guard's own connection is going away
            self._failed(target, method, session_id, str(exc) or type(exc).__name__, fail_closed_note)
            return False
        log.debug(
            "CDP guard: <- %s session=%s ok in %.0fms %s",
            method,
            session_id,
            (loop.time() - started) * 1000,
            json.dumps(result)[:200] if result else "",
        )
        return True

    def _gone(self, target: GuardTarget | None, method: str, session_id: str | None, why: str) -> None:
        # The target closed (or its session ended) under us: nothing is left to guard.
        log.debug("CDP guard: %s session=%s: target already gone (%s)", method, session_id, why)
        if target is not None:
            target.state, target.error = "gone", why

    def _failed(
        self,
        target: GuardTarget | None,
        method: str,
        session_id: str | None,
        error: str,
        note: str = "",
    ) -> None:
        # Fail closed: nothing more is sent for this target, which stays paused.
        if target is not None:
            target.state, target.error = "failed", error
        where = target.describe(session_id or "", time.monotonic()) if target else f"session {session_id}"
        log.warning(
            "CDP guard %s failed (%s): %s %s (fail closed)", method, error, where, note or "left paused"
        )

    def _end_session(
        self, target: GuardTarget | None, method: str, session_id: str | None, error: str
    ) -> None:
        """A running target lacks a protection the guard could not confirm: end the browser."""
        if target is not None:
            target.state, target.error = "failed", error
        where = target.describe(session_id or "", time.monotonic()) if target else f"session {session_id}"
        log.error(
            "CDP guard %s failed after the resume (%s): %s; ending the browser session (fail closed)",
            method,
            error,
            where,
        )
        if self._closed:
            return
        self._closed = True  # alive is now False: the engine refuses every further action
        if self.on_lost is not None:
            try:
                self.on_lost()
            except Exception:
                log.exception("browser CDP guard: on_lost failed")

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


def _kill_group_now(pid: int) -> None:
    """SIGKILL a process group at once (synchronous: safe to call from any callback)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


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
        action_timeout_s: float = DEFAULT_ACTION_TIMEOUT_S,
        work_root: str | None = None,
        chrome_extra_args: Sequence[str] = (),
        start_timeout_s: float = START_TIMEOUT_S,
    ) -> None:
        self.binary = binary
        self.chrome = chrome
        self.proxy = proxy
        self.action_timeout_s = check_action_timeout(action_timeout_s)  # refuse at startup
        self.work_root = Path(work_root or tempfile.gettempdir())
        self.chrome_extra_args = check_chrome_extra_args(chrome_extra_args)  # refuse at startup
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
            action_timeout_s=action_timeout_from_env(env),
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
        profile = workdir / "profile"
        argv = chrome_argv(
            self.chrome,
            proxy=self.proxy,
            user_data_dir=str(profile),
            viewport=viewport,
            extra=self.chrome_extra_args,
        )
        chrome = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env(),
            start_new_session=True,  # its own process group, so stop() reaps the helpers too
        )
        guard: CdpGuard | None = None
        try:
            port, path = await self._wait_for_cdp(chrome, profile)
            # Losing the guard means pages could run unguarded: kill that Chrome right away.
            guard = CdpGuard(f"ws://127.0.0.1:{port}{path}", on_lost=lambda: _kill_group_now(chrome.pid))
            await guard.start()
        except BaseException:
            if guard is not None:
                await guard.close()
            await _kill_group(chrome)
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        self._sessions[session_id] = _AbSession(session_id, workdir, port, chrome, guard)
        log.info("browser session %s started (cdp %s, policy %s)", session_id, port, policy)

    async def _wait_for_cdp(self, chrome: asyncio.subprocess.Process, profile: Path) -> tuple[int, str]:
        """Chrome's own debugging port and browser path, read from its ``DevToolsActivePort``.

        The file lives in this session's fresh profile, so the port is this Chrome's and no
        other session's (no free-port guess that another process could take in between).
        """
        marker = profile / "DevToolsActivePort"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.start_timeout_s
        while True:
            if chrome.returncode is not None:
                raise RuntimeError(f"Chrome exited with status {chrome.returncode} on start")
            with contextlib.suppress(OSError, UnicodeDecodeError):
                parsed = parse_devtools_active_port(marker.read_text())
                if parsed is not None:
                    return parsed
            if loop.time() > deadline:
                raise RuntimeError("Chrome did not open its debugging port in time")
            await asyncio.sleep(0.05)

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
                env=child_env(),
            )
        except OSError as exc:
            return CliReply(ok=False, error=f"agent-browser could not run: {exc.strerror or exc}")
        run = await collect_process(proc, limit_s)
        if run.timed_out:
            # What agent-browser had said by then, and which targets the guard still holds, are
            # what tells a slow page from a hang (scripts/tests/browser-diagnose.sh reads this).
            log.warning(
                "agent-browser %s timed out after %.1fs (session %s, limit %.0fs); stderr: %s; "
                "partial stdout: %s; targets the guard has not let run: %s",
                command[0] if command else "?",
                run.elapsed_s,
                s.session_id,
                limit_s,
                run.stderr.decode("utf-8", "replace").strip()[-2000:] or "-",
                run.stdout.decode("utf-8", "replace").strip()[-1000:] or "-",
                "; ".join(_guard_stuck(s.guard)) or "none",
            )
            return CliReply(ok=False, error=f"timed out after {limit_s:.0f}s", timed_out=True)
        log.debug(
            "agent-browser %s exited %s in %.1fs (session %s)",
            command[0] if command else "?",
            run.returncode,
            run.elapsed_s,
            s.session_id,
        )
        return parse_cli_output(run.stdout, run.stderr, run.returncode)

    async def act(self, session_id: str, action: str, args: dict[str, Any]) -> ActionResult:
        s = self._session(session_id)
        command = build_command(action, args)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + action_budget(action, self.action_timeout_s)

        def left(share: float = 1.0) -> float:
            return max(0.0, (deadline - loop.time()) * share)

        limit = command_limit(action, self.action_timeout_s)
        reply = await self._cli(s, command, limit_s=limit)
        error = reply.error
        if reply.timed_out:
            error = timeout_message(action, limit)
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
            snapshot=wrap_boundaries(content, boundary, origin=origin, max_chars=MAX_PAGE_CHARS),
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
        """The port this session's own agent-browser daemon reports for its stream server.

        Never a port guessed here: a guessed free port could be bound by another session's
        stream server in between, and the viewer would watch the wrong browser.
        """
        async with s.stream_lock:
            if s.stream_port is None:
                status = await self._cli(s, ["stream", "status"], limit_s=10)
                port = stream_port_from(status.data) if status.ok else None
                if port is None:
                    enabled = await self._cli(s, ["stream", "enable"], limit_s=10)
                    if not enabled.ok:
                        raise RuntimeError(f"cannot start the live view: {enabled.error}")
                    status = await self._cli(s, ["stream", "status"], limit_s=10)
                    port = stream_port_from(status.data) if status.ok else None
                if port is None:
                    raise RuntimeError("cannot start the live view: agent-browser reported no stream port")
                s.stream_port = port
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
