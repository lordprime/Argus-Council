"""Real-browser integration test: the composed ``browser`` and ``egress`` containers.

Skipped unless ``ARGUS_TEST_BROWSER_URL`` is set. ``scripts/tests/browser-integration.sh`` starts
the stack (browser, egress and two test sites) and sets everything below; from ``backend/``:

    ARGUS_TEST_BROWSER_URL=unix:/path/browser.sock ARGUS_TEST_BROWSER_TOKEN=... \\
      uv run pytest -m browser tests/integration/test_browser_real.py -v

- ``ARGUS_TEST_BROWSER_URL`` / ``ARGUS_TEST_BROWSER_TOKEN``: the browser service.
- ``ARGUS_TEST_SITE_URL`` / ``ARGUS_TEST_SITE_LOG``: a site reachable through egress (over https,
  so a request the browser sends is never stopped by egress's GET-only rule for plain http) and
  the directory where it logs every request it receives.
- ``ARGUS_TEST_CANARY_URL`` / ``ARGUS_TEST_CANARY_LOG``: a site on a private address that egress
  can route to and must refuse.
- ``ARGUS_TEST_BROWSER_EXEC``: a command prefix that runs a command in the browser container
  (``docker compose ... exec -T browser``), for the checks on its disk and on agent-browser.

These are the hard gate for the browser's security properties: read-only (no form is submitted,
not even through a service worker), no downloads, agent-browser's own action policy, and no
private addresses.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from argus.browser.client import ActionRejected, RemoteBrowser, SessionLost

BROWSER_URL = os.environ.get("ARGUS_TEST_BROWSER_URL", "")
BROWSER_TOKEN = os.environ.get("ARGUS_TEST_BROWSER_TOKEN", "")
SITE_URL = os.environ.get("ARGUS_TEST_SITE_URL", "").rstrip("/")
SITE_LOG = os.environ.get("ARGUS_TEST_SITE_LOG", "")
CANARY_URL = os.environ.get("ARGUS_TEST_CANARY_URL", "").rstrip("/")
CANARY_LOG = os.environ.get("ARGUS_TEST_CANARY_LOG", "")
EXEC = shlex.split(os.environ.get("ARGUS_TEST_BROWSER_EXEC", ""))

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        not BROWSER_URL, reason="needs a running browser service: scripts/tests/browser-integration.sh"
    ),
]

needs_site = pytest.mark.skipif(
    not (SITE_URL and SITE_LOG), reason="needs ARGUS_TEST_SITE_URL and ARGUS_TEST_SITE_LOG (test site)"
)
needs_exec = pytest.mark.skipif(
    not EXEC,
    reason=(
        "needs ARGUS_TEST_BROWSER_EXEC (e.g. 'docker compose -p argus-browser-test ... exec -T browser') "
        "to inspect the browser container"
    ),
)

# agent-browser snapshot lines look like  - link "Next page" [ref=e3]  or, with attributes,
#   - checkbox "I agree" [checked=false, ref=e2]
_SNAPSHOT_LINE = re.compile(r'^\s*-\s*(\w+)\s+"([^"]*)"[^\n]*?\bref=(e\d+)\b', re.MULTILINE)
FORBIDDEN = ("fill", "type", "press", "select", "upload", "download", "eval")


def find_ref(snapshot: str, name: str) -> str:
    for _role, label, ref in _SNAPSHOT_LINE.findall(snapshot):
        if name in label:
            return "@" + ref
    raise AssertionError(f"no element named {name!r} in the snapshot:\n{snapshot}")


class SiteLog:
    """The requests a test site received, read from its ``requests.jsonl``."""

    def __init__(self, directory: str) -> None:
        self.path = Path(directory) / "requests.jsonl"
        self.dir = Path(directory)

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def mark(self) -> int:
        return len(self.entries())

    def since(self, mark: int) -> list[dict[str, Any]]:
        return self.entries()[mark:]


async def poll(check: Callable[[], Any], timeout: float, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() > deadline:
            return value
        await asyncio.sleep(interval)


def run_in_browser(*argv: str, timeout: float = 120) -> str:
    proc = subprocess.run([*EXEC, *argv], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603
    assert proc.returncode == 0, f"{argv[0]} in the browser container failed: {proc.stderr[-2000:]}"
    return proc.stdout


@pytest.fixture
def browser() -> RemoteBrowser:
    return RemoteBrowser(BROWSER_URL, BROWSER_TOKEN, timeout_s=90)


@pytest.fixture
async def session(browser: RemoteBrowser) -> AsyncIterator[str]:
    sid = await browser.open_session(f"it-{uuid.uuid4().hex[:12]}", "member-1")
    try:
        yield sid
    finally:
        await browser.close_session(sid)


@pytest.fixture
def site_log() -> SiteLog:
    return SiteLog(SITE_LOG)


# ---------------------------------------------------------------- the brief's four cases


@needs_site
async def test_reads_a_page_and_follows_a_link(browser: RemoteBrowser, session: str):
    page = await browser.act(session, "open", {"url": f"{SITE_URL}/"})
    assert not page["error"], page["error"]
    assert page["url"].startswith(SITE_URL), page["url"]
    assert "Argus test site" in page["title"]
    assert "AGENT_BROWSER_PAGE_CONTENT" in page["snapshot"]  # page text stays inside boundaries
    assert base64.b64decode(page["screenshot_b64"])[:2] == b"\xff\xd8"  # a JPEG

    clicked = await browser.act(session, "click", {"ref": find_ref(page["snapshot"], "Next page")})
    assert not clicked["error"], clicked["error"]
    assert clicked["url"] == f"{SITE_URL}/next.html"
    assert clicked["title"] == "Second page"

    back = await browser.act(session, "back", {})
    assert back["url"].rstrip("/") == SITE_URL


@needs_site
async def test_forward_wait_new_tab_and_close(browser: RemoteBrowser):
    """History both ways, wait, a target=_blank link (the session follows the new tab and closes
    the old one), and closing the session: later actions get "session lost"."""
    sid = await browser.open_session(f"it-{uuid.uuid4().hex[:12]}", "member-tabs")
    page = await browser.act(sid, "open", {"url": f"{SITE_URL}/"})
    await browser.act(sid, "click", {"ref": find_ref(page["snapshot"], "Next page")})
    back = await browser.act(sid, "back", {})
    assert back["url"].rstrip("/") == SITE_URL, back["url"]
    forward = await browser.act(sid, "forward", {})
    assert forward["url"] == f"{SITE_URL}/next.html", forward["url"]
    waited = await browser.act(sid, "wait", {"ms": 500})
    assert not waited["error"], waited["error"]
    assert waited["url"] == f"{SITE_URL}/next.html"

    tabs = await browser.act(sid, "open", {"url": f"{SITE_URL}/tabs.html"})
    clicked = await browser.act(sid, "click", {"ref": find_ref(tabs["snapshot"], "Open in new tab")})
    assert not clicked["error"], clicked["error"]
    assert clicked["url"] == f"{SITE_URL}/next.html", f"the session did not follow the new tab: {clicked}"
    assert clicked["title"] == "Second page"
    again = await browser.act(sid, "snapshot", {})
    assert again["url"] == f"{SITE_URL}/next.html", "the old tab came back"

    await browser.close_session(sid)  # errors are not suppressed here
    with pytest.raises(SessionLost):
        await browser.act(sid, "snapshot", {})


@needs_site
async def test_prefilled_form_submit_does_not_post(browser: RemoteBrowser, session: str, site_log: SiteLog):
    page = await browser.act(session, "open", {"url": f"{SITE_URL}/form.html"})
    assert not page["error"], page["error"]

    # Control: a script's fetch POST is not blocked (by design), so a POST from this page can
    # reach the site. Without it, "the site saw no POST" below would prove nothing.
    mark = site_log.mark()
    await browser.act(session, "click", {"ref": find_ref(page["snapshot"], "Script POST")})
    control = await poll(
        lambda: [e for e in site_log.since(mark) if e["method"] == "POST" and e["path"] == "/control-post"],
        10,
    )
    assert control, f"the control POST never reached the site: {site_log.since(mark)}"

    page = await browser.act(session, "open", {"url": f"{SITE_URL}/form.html"})
    mark = site_log.mark()
    submitted = await browser.act(session, "click", {"ref": find_ref(page["snapshot"], "Send form")})
    await asyncio.sleep(3)  # let a request that did leave the browser land
    seen = site_log.since(mark)
    assert not [e for e in seen if e["path"].startswith("/submit")], f"the form was sent: {seen}"
    assert "POST RECEIVED" not in submitted["snapshot"]


# What only egress says when it refuses: its 403 reasons for plain http (Chrome shows the body as
# the page), and Chrome's error for a refused CONNECT (https, WebSockets). A plain connection
# error (ERR_CONNECTION_REFUSED and the like) would mean the browser went around the proxy.
EGRESS_REFUSALS = (
    "destination resolves to a disallowed address",
    "plain http is only forwarded on port 80",
    "only ports 80 and 443 are allowed",
    "ERR_TUNNEL_CONNECTION_FAILED",
)
REACHED_DIRECTLY = ("ERR_CONNECTION_REFUSED", "ERR_CONNECTION_RESET", "ERR_ADDRESS_UNREACHABLE")


async def assert_refused_by_egress(browser: RemoteBrowser, session: str, url: str) -> None:
    result = await browser.act(session, "open", {"url": url})
    text = (await browser.act(session, "get_text", {}))["snapshot"]
    seen = f"{result['error'] or ''}\n{result['snapshot']}\n{text}"
    assert any(reason in seen for reason in EGRESS_REFUSALS), (
        f"{url} was not refused by egress: error={result['error']!r} page={text[:600]!r}"
    )
    for direct in REACHED_DIRECTLY:
        assert direct not in seen, f"{url} was tried without the proxy ({direct}): {seen[:600]!r}"
    assert "webSocketDebuggerUrl" not in seen, f"{url} served Chrome's DevTools JSON: {seen[:600]!r}"
    assert "CANARY-REACHED" not in seen, url


@needs_site
async def test_private_address_is_unreachable(browser: RemoteBrowser, session: str):
    canary = SiteLog(CANARY_LOG) if CANARY_LOG else None
    targets = ["http://10.0.0.1/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/"]
    if CANARY_URL:
        targets += [f"{CANARY_URL}/", CANARY_URL.replace("https://", "http://", 1) + "/"]
    for url in targets:
        await assert_refused_by_egress(browser, session, url)
    if canary is not None:
        assert canary.entries() == [], f"the private canary site was reached: {canary.entries()}"


_PORTS_PROBE = r"""
import glob, json, subprocess, sys
sid = sys.argv[1]
[port_file] = glob.glob(f"/tmp/argus-browser/argus-browser-{sid}-*/profile/DevToolsActivePort")
[policy] = glob.glob("/tmp/argus-browser/argus-browser-policy-*.json")
devtools_port, devtools_path = open(port_file).read().split()[:2]

def ab(*command):
    argv = ["/usr/local/bin/agent-browser", "--cdp", devtools_port, "--session", sid, "--json",
            "--action-policy", policy, *command]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
    return (json.loads(out) if out.strip() else {}).get("data") or {}

status = ab("stream", "status")
if not status.get("enabled"):
    ab("stream", "enable")
    status = ab("stream", "status")
print(json.dumps({"devtools_port": int(devtools_port), "devtools_path": devtools_path,
                  "stream_port": status.get("port")}))
"""


async def ws_probe(browser: RemoteBrowser, session: str, target: str) -> str:
    """Open the site's WebSocket probe page (plain http) against ``target``; its outcome line."""
    http_site = SITE_URL.replace("https://", "http://", 1)
    await browser.act(session, "open", {"url": f"{http_site}/ws-probe.html?url={quote(target, safe='')}"})
    text = ""
    for _ in range(20):
        text = (await browser.act(session, "get_text", {}))["snapshot"]
        if "ws: pending" not in text:
            break
        await browser.act(session, "wait", {"ms": 500})
    assert "protocol: http:" in text, f"the probe page did not load over plain http: {text[:400]!r}"
    return text


@needs_site
@needs_exec
async def test_loopback_services_in_the_browser_container_are_unreachable(
    browser: RemoteBrowser, session: str
):
    """Chrome's DevTools port and agent-browser's stream server listen on loopback inside the
    browser container. Pages must not reach them: loopback goes to egress like everything else."""
    await browser.act(session, "open", {"url": f"{SITE_URL}/"})
    ports = json.loads(run_in_browser("python", "-c", _PORTS_PROBE, session))
    devtools, stream = ports["devtools_port"], ports["stream_port"]
    assert isinstance(stream, int), f"agent-browser reported no stream port: {ports}"

    for url in (
        f"http://127.0.0.1:{devtools}/json/version",
        f"http://localhost:{devtools}/json/version",
        f"https://127.0.0.1:{devtools}/json/version",
        f"http://127.0.0.1:{stream}/",
        f"https://127.0.0.1:{stream}/",
    ):
        await assert_refused_by_egress(browser, session, url)

    # Positive control: from this page a WebSocket to the site itself opens (through egress).
    control = await ws_probe(browser, session, f"ws://{SITE_URL.split('://', 1)[1]}/ws-echo")
    assert "ws: OPEN" in control, f"the control WebSocket did not open: {control[:400]!r}"
    for target in (f"ws://127.0.0.1:{devtools}{ports['devtools_path']}", f"ws://127.0.0.1:{stream}/"):
        text = await ws_probe(browser, session, target)
        assert "ws: OPEN" not in text, f"a page opened a WebSocket to {target}"
        assert "ws: failed" in text, f"the WebSocket probe to {target} did not finish: {text[:400]!r}"


@needs_site
async def test_frames_arrive(browser: RemoteBrowser, session: str):
    page = await browser.act(session, "open", {"url": f"{SITE_URL}/"})
    assert not page["error"], page["error"]

    async def first_frame() -> bytes:
        async for frame in browser.frames(session, 4):
            return frame
        raise AssertionError("the frame stream ended without a frame")

    async def nudge() -> None:  # an idle page may not repaint; a scroll makes it
        await asyncio.sleep(1)
        with contextlib.suppress(Exception):
            await browser.act(session, "scroll", {"direction": "down", "px": 200})

    nudger = asyncio.create_task(nudge())
    try:
        frame = await asyncio.wait_for(first_frame(), 5)
    finally:
        nudger.cancel()
        with contextlib.suppress(BaseException):
            await nudger
    assert frame[:2] == b"\xff\xd8", frame[:16]


# ---------------------------------------------------------------- the security gates


@needs_site
async def test_service_worker_form_submit_does_not_post(
    browser: RemoteBrowser, session: str, site_log: SiteLog
):
    """A pass-through service worker must not carry a form POST past the request guard."""
    mark = site_log.mark()
    await browser.act(session, "open", {"url": f"{SITE_URL}/sw.html"})

    async def status() -> str:
        return (await browser.act(session, "get_text", {}))["snapshot"]

    deadline = time.monotonic() + 20
    text = await status()
    while "controlled: yes" not in text and time.monotonic() < deadline:
        await browser.act(session, "wait", {"ms": 1000})
        text = await status()
    assert "sw: active" in text, f"the service worker never activated (test page): {text}"
    assert any(e["path"] == "/sw.js" for e in site_log.since(mark)), "the worker script was never fetched"
    assert "controlled: yes" in text, f"the worker never took control of the page: {text}"

    # Positive control: a script POST from this page reaches the site. `via_worker` (a header the
    # worker adds) says whether the worker forwarded it or the page's requests bypass the worker.
    page = await browser.act(session, "snapshot", {})
    mark = site_log.mark()
    await browser.act(session, "click", {"ref": find_ref(page["snapshot"], "Script POST via worker")})
    control = await poll(
        lambda: [
            e for e in site_log.since(mark) if e["method"] == "POST" and e["path"] == "/sw-control-post"
        ],
        10,
    )
    assert control, f"the control POST never reached the site: {site_log.since(mark)}"
    via_worker = control[0].get("via_worker")

    page = await browser.act(session, "snapshot", {})
    mark = site_log.mark()
    submitted = await browser.act(session, "click", {"ref": find_ref(page["snapshot"], "Send via worker")})
    await asyncio.sleep(3)
    seen = site_log.since(mark)
    assert not [e for e in seen if e["path"].startswith("/sw-submit")], (
        f"the form was sent (page controlled by a pass-through worker; worker forwards script POSTs: "
        f"{via_worker}): {seen}"
    )
    assert "POST RECEIVED" not in submitted["snapshot"]


async def test_service_refuses_write_actions(browser: RemoteBrowser, session: str):
    args = {"ref": "@e1", "text": "x", "value": "x", "key": "Enter", "url": "https://example.com", "js": "1"}
    for action in FORBIDDEN:
        with pytest.raises(ActionRejected):
            await browser.act(session, action, args)


@needs_site
@needs_exec
async def test_download_link_leaves_no_file(browser: RemoteBrowser, session: str, site_log: SiteLog):
    download = json.loads((site_log.dir / "download.json").read_text())
    page = await browser.act(session, "open", {"url": f"{SITE_URL}/download.html"})
    assert not page["error"], page["error"]
    mark = site_log.mark()
    await browser.act(session, "click", {"ref": find_ref(page["snapshot"], "Download report")})
    await asyncio.sleep(5)  # long enough for a 2 KB download to finish if it were allowed

    # Everything the browser user can write: /tmp (HOME, Chrome profiles, agent-browser, the
    # service's workdir) and the socket directory. The body token appears only in the body.
    found = run_in_browser(
        "sh",
        "-c",
        'find /tmp /run/argus-browser \\( -name "*$1*" -o -name "*.crdownload" \\) -print 2>/dev/null; '
        'grep -rlsF -- "$2" /tmp /run/argus-browser 2>/dev/null; true',
        "sh",
        download["filename"],
        download["body_token"],
    )
    assert found.strip() == "", (
        f"a download reached the browser's disk: {found} (site saw: {site_log.since(mark)})"
    )


_CLI_PROBE = r"""
import glob, json, subprocess, sys
sid = sys.argv[1]
binary = "/usr/local/bin/agent-browser"
ports = glob.glob(f"/tmp/argus-browser/argus-browser-{sid}-*/profile/DevToolsActivePort")
policies = glob.glob("/tmp/argus-browser/argus-browser-policy-*.json")
if len(ports) != 1 or len(policies) != 1:
    print(json.dumps({"error": f"ports={ports} policies={policies}"}))
    sys.exit(0)
port = open(ports[0]).read().split()[0]
commands = {
    "fill": ["fill", "@e1", "x"],
    "type": ["type", "@e1", "x"],
    "press": ["press", "Enter"],
    "select": ["select", "@e1", "x"],
    "check": ["check", "@e1"],
    "upload": ["upload", "@e1", "/etc/hostname"],
    "download": ["download", "@e1", "/tmp/argus-probe.bin"],
    "eval": ["eval", "1+1"],
    "keyboard": ["keyboard", "type", "x"],
    "wait_fn": ["wait", "--fn", "true"],
    "get_url": ["get", "url"],
}
out = {"policy": open(policies[0]).read()}
for name, command in commands.items():
    argv = [binary, "--cdp", port, "--session", sid, "--json", "--action-policy", policies[0], *command]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    out[name] = {"rc": proc.returncode, "stdout": proc.stdout[-1500:], "stderr": proc.stderr[-1500:]}
print(json.dumps(out))
"""


CLI_REFUSED = (
    "fill",
    "type",
    "press",
    "select",
    "check",
    "upload",
    "download",
    "eval",
    "keyboard",
    "wait_fn",
)


@needs_site
@needs_exec
async def test_agent_browser_cli_refuses_write_actions(browser: RemoteBrowser, session: str):
    """The policy file the service writes is accepted by the real agent-browser, which refuses the
    write actions itself, even when they are sent straight to its CLI (bypassing the service)."""
    page = await browser.act(session, "open", {"url": f"{SITE_URL}/form.html"})
    assert not page["error"], page["error"]
    out = json.loads(run_in_browser("python", "-c", _CLI_PROBE, session))
    assert "error" not in out, out.get("error")

    # An allowed command works through the same daemon, policy and flags: the policy loaded.
    allowed = json.loads(out["get_url"]["stdout"])
    assert allowed.get("success") is True, out["get_url"]
    for name in CLI_REFUSED:
        reply = out[name]
        try:
            payload = json.loads(reply["stdout"])
        except ValueError:
            payload = {}
        error = json.dumps(payload.get("error")) if payload else reply["stdout"] + reply["stderr"]
        assert payload.get("success") is False, f"{name} was not refused: {reply}"
        assert "denied by policy" in error, f"{name} failed for another reason: {reply}"
        assert "failed to load" not in error, f"agent-browser did not accept the policy file: {reply}"

    # The service's own session is still usable afterwards.
    after = await browser.act(session, "snapshot", {})
    assert not after["error"], after["error"]
    assert "Send form" in after["snapshot"]
