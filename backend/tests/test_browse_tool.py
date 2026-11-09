"""The browse tool: read-only live browsing through the browser service."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

from argus.browser.client import ActionRejected, BrowserBusy, RemoteBrowser, SessionLost
from argus.core.config import Settings
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from argus.tools import browser as browser_mod
from argus.tools.browser import Browse, browser_tools

JPEG = base64.b64encode(b"\xff\xd8jpeg").decode()


async def public(host):
    return ["93.184.216.34"]


def page(url="https://example.com/", title="Ex", snapshot="- link @e1", shot=JPEG, error=None):
    return {"url": url, "title": title, "snapshot": snapshot, "screenshot_b64": shot, "error": error}


class FakeBrowser:
    def __init__(self, results=None):
        self.calls = []
        self.opened = []
        self.closed_runs = []
        self.results = list(results or [])
        self.raise_next = []

    async def open_session(self, run_id, member_id):
        self.calls.append(("open_session", member_id))
        self.opened.append(member_id)
        return f"sess-{len(self.opened)}"

    async def act(self, session_id, action, args):
        self.calls.append((session_id, action, args))
        if self.raise_next:
            raise self.raise_next.pop(0)
        if self.results:
            return self.results.pop(0)
        return page()

    async def close_session(self, session_id):
        self.calls.append(("close_session", session_id))

    async def close_run(self, run_id):
        self.closed_runs.append(run_id)


class Ctx:
    def __init__(self, allow=None):
        self.events = []
        self.shots = []
        self.ledger = []

        async def emit(type_, data, **kw):
            self.events.append((type_, data))

        async def write_binary(path, data, *, by, mime, kind="export"):
            self.shots.append((path, data, by, mime, kind))

        rails = Guardrails()
        if allow is not None:
            rails.network.allow_domains = allow
        self.run = SimpleNamespace(
            engine=GuardrailEngine(rails),
            sources={},
            sink=SimpleNamespace(emit=emit),
            extra={"closers": []},
            run_id="r1",
            owner_id="o1",
            workspace=SimpleNamespace(write_binary=write_binary),
            sessionmaker=None,
        )
        self.step_id = "s1"
        self.member_id = "m1"
        self.member_name = "Researcher"
        self.role = "researcher"
        self.tool_call_id = "t1"


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    async def record_call(ctx, **entry):
        ctx.ledger.append(entry)

    monkeypatch.setattr(browser_mod, "record_call", record_call)


def make(fake=None, **kw):
    fake = fake or FakeBrowser()
    return Browse(fake, resolver=public, **kw), fake


async def test_open_checks_domain_before_contacting_service():
    tool, fake = make()
    res = await tool.invoke({"action": "open", "url": "https://x.com/"}, Ctx(["*.gov"]))
    assert res.is_error and "not in the allowed domains" in res.content
    assert fake.calls == []


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "data:text/html,hi"]
)
async def test_rejects_non_http_urls(url):
    tool, fake = make()
    res = await tool.invoke({"action": "open", "url": url}, Ctx())
    assert res.is_error
    assert fake.calls == []


async def test_click_that_leaves_allowed_domains_goes_back():
    fake = FakeBrowser(
        [
            page("https://a.gov/", "A", "x"),
            page("https://x.com/p", "X", "y"),
            page("https://a.gov/", "A", "x", shot=None),
        ]
    )
    tool, _ = make(fake)
    ctx = Ctx(["*.gov"])
    await tool.invoke({"action": "open", "url": "https://a.gov/"}, ctx)
    res = await tool.invoke({"action": "click", "ref": "@e1"}, ctx)
    assert res.is_error and "Domain 'x.com' is not in the allowed domains" in res.content
    assert fake.calls[-1][1] == "back"


async def opened(tool, ctx):
    await tool.invoke({"action": "open", "url": "https://a.gov/"}, ctx)


async def test_error_result_on_disallowed_url_goes_back_with_domain_error():
    fake = FakeBrowser(
        [
            page("https://a.gov/", "A", "x"),
            page("https://x.com/p", "X", "", error="boom"),
            page("https://a.gov/"),
        ]
    )
    tool, _ = make(fake)
    ctx = Ctx(["*.gov"])
    await opened(tool, ctx)
    res = await tool.invoke({"action": "click", "ref": "@e1"}, ctx)
    assert res.is_error and "Domain 'x.com' is not in the allowed domains" in res.content
    assert fake.calls[-1][1] == "back"
    assert any(t == "browser.action" and d["action"] == "back" for t, d in ctx.events)


async def test_empty_url_with_snapshot_is_an_error_and_not_cited():
    fake = FakeBrowser([page("", "T", "secret page text")])
    tool, _ = make(fake)
    ctx = Ctx()
    res = await tool.invoke({"action": "snapshot"}, ctx)
    assert res.is_error and "secret" not in res.content
    assert ctx.run.sources == {}
    assert not [c for c in fake.calls if len(c) == 3 and c[1] == "back"]


async def _closed_session_case(second):
    fake = FakeBrowser([page("https://a.gov/"), page("https://x.com/p")])
    if second != "raise":
        fake.results.append(page("https://y.com/"))
    tool, _ = make(fake)
    ctx = Ctx(["*.gov"])
    await opened(tool, ctx)
    if second == "raise":
        orig = fake.act

        async def act(session_id, action, args):
            if action == "back":
                raise RuntimeError("down")
            return await orig(session_id, action, args)

        fake.act = act
    res = await tool.invoke({"action": "click", "ref": "@e1"}, ctx)
    assert res.is_error and "not in the allowed domains" in res.content
    assert ("close_session", "sess-1") in fake.calls
    assert "m1" not in ctx.run.extra["browser_sessions"]
    assert "m1" not in ctx.run.extra.get("browser_urls", {})
    assert ("browser.session.closed", {"member_id": "m1", "session_id": "sess-1"}) in ctx.events
    assert any(r["method"] == "session.close" for r in ctx.ledger)


async def test_back_raising_closes_the_session():
    await _closed_session_case("raise")


async def test_back_landing_on_another_disallowed_page_closes_the_session():
    await _closed_session_case("again")


async def test_run_end_closer_writes_session_close_ledger_row():
    tool, _ = make()
    ctx = Ctx()
    await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    await ctx.run.extra["closers"][0]()
    closes = [r for r in ctx.ledger if r["method"] == "session.close"]
    assert closes and closes[0]["detail"]["member_id"] == "m1"


async def test_lost_session_only_reopens_for_open_actions():
    fake = FakeBrowser()
    tool, _ = make(fake)
    ctx = Ctx()
    await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    fake.raise_next = [SessionLost()]
    res = await tool.invoke({"action": "click", "ref": "e1"}, ctx)
    assert res.is_error and res.content == "The browser session was restarted; open the page again."
    assert fake.opened == ["m1", "m1"]
    assert not any(len(c) == 3 and c[1] == "click" and c[0] == "sess-2" for c in fake.calls)
    assert any(r["method"] == "session.close" for r in ctx.ledger)


@pytest.mark.parametrize("url", ["about:blank", "chrome-error://chromewebdata/", ""])
async def test_no_page_open(url):
    fake = FakeBrowser([page(url, "", "")])
    tool, _ = make(fake)
    res = await tool.invoke({"action": "back"}, Ctx())
    assert res.is_error and res.content == "No page is open; use action open."
    assert [c for c in fake.calls if len(c) == 3 and c[1] == "back"] == [("sess-1", "back", {})]


async def test_one_session_per_member_and_closer_registered():
    tool, fake = make()
    ctx = Ctx()
    await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    await tool.invoke({"action": "snapshot"}, ctx)
    assert fake.opened == ["m1"]
    ctx.member_id = "m2"
    await tool.invoke({"action": "snapshot"}, ctx)
    assert fake.opened == ["m1", "m2"]
    assert len(ctx.run.extra["closers"]) == 1
    assert ctx.run.extra["browser_sessions"] == {"m1": "sess-1", "m2": "sess-2"}
    await ctx.run.extra["closers"][0]()
    assert fake.closed_runs == ["r1"]


async def test_page_is_cited_and_screenshot_saved():
    tool, _ = make()
    ctx = Ctx()
    res = await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    assert not res.is_error
    assert "https://example.com/" in ctx.run.sources
    assert res.content.startswith("[Source 1] Ex — https://example.com/")
    assert "- link @e1" in res.content and "(screenshot kept)" in res.content
    path, data, by, mime, kind = ctx.shots[0]
    assert path == "browser/m1/1.jpg" and kind == "screenshot" and mime == "image/jpeg"
    assert data == b"\xff\xd8jpeg" and by == "Researcher"
    saved = {"member_id": "m1", "path": path, "url": "https://example.com/", "n": 1}
    assert ("browser.snapshot.saved", saved) in ctx.events


async def test_screenshot_cap():
    tool, _ = make(screenshots_per_member=2)
    ctx = Ctx()
    for _i in range(3):
        await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    assert len(ctx.shots) == 2
    saved = [d for t, d in ctx.events if t == "browser.snapshot.saved"]
    assert saved[-1]["kept"] is False and saved[-1]["reason"] == "limit"


async def test_busy_and_lost_sessions():
    class Busy(FakeBrowser):
        async def open_session(self, run_id, member_id):
            raise BrowserBusy()

    tool, _ = make(Busy())
    res = await tool.invoke({"action": "open", "url": "https://example.com/"}, Ctx())
    assert res.is_error and "browser busy, use web_fetch or try later" in res.content

    fake = FakeBrowser()
    tool, _ = make(fake)
    ctx = Ctx()
    await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    fake.raise_next = [SessionLost()]
    res = await tool.invoke({"action": "open", "url": "https://example.com/two"}, ctx)
    assert not res.is_error
    assert fake.opened == ["m1", "m1"]
    assert ctx.run.extra["browser_sessions"]["m1"] == "sess-2"


async def test_ledger_rows_and_no_http_row():
    tool, _ = make()
    ctx = Ctx()
    await tool.invoke({"action": "open", "url": "https://example.com/"}, ctx)
    rows = [r for r in ctx.ledger if r["method"] == "open"]
    assert rows and rows[0]["protocol"] == "browser" and rows[0]["peer"] == "example.com"
    assert rows[0]["direction"] == "outbound"
    assert any(r["method"] == "session.open" for r in ctx.ledger)
    assert all(r["protocol"] == "browser" for r in ctx.ledger)
    from argus.runtime.agent import _LEDGERED_SOURCES

    assert tool.source in _LEDGERED_SOURCES and tool.risk == frozenset({"network"})


async def test_bot_check_page_is_reported():
    fake = FakeBrowser([page(title="Just a moment...", snapshot="checking", shot=None)])
    tool, _ = make(fake)
    res = await tool.invoke({"action": "open", "url": "https://example.com/"}, Ctx())
    assert res.is_error and "bot check" in res.content and "example.com" in res.content


def test_tool_absent_without_browser_url():
    assert browser_tools(Settings(browser_url=None)) == []
    tools = browser_tools(Settings(browser_url="http://browser:8000", browser_token="t"))
    assert [t.name for t in tools] == ["browse"]


async def test_pages_are_captured_to_the_library(monkeypatch):
    seen = []

    async def capture(self, ctx, url, title, text):
        seen.append((url, title, text))

    monkeypatch.setattr(Browse, "_capture", capture)
    tool, _ = make(capture=True)
    await tool.invoke({"action": "open", "url": "https://example.com/"}, Ctx())
    assert seen == [("https://example.com/", "Ex", "- link @e1")]


async def test_client_maps_requests_and_statuses():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers.get("authorization"), req.content))
        if req.url.path == "/sessions" and req.method == "POST":
            return httpx.Response(201, json={"session_id": "s9"})
        if req.url.path == "/sessions/s9/actions":
            return httpx.Response(200, json=page("u", "t", "s", shot=None))
        if req.url.path == "/sessions/gone/actions":
            return httpx.Response(404, json={})
        if req.url.path == "/runs/r1/sessions":
            return httpx.Response(200, json={"closed": 1})
        return httpx.Response(204)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RemoteBrowser("http://browser", "tok", http)
    assert await client.open_session("r1", "m1") == "s9"
    out = await client.act("s9", "click", {"ref": "@e1"})
    assert out["url"] == "u"
    with pytest.raises(SessionLost):
        await client.act("gone", "snapshot", {})
    await client.close_session("s9")
    await client.close_run("r1")
    assert seen[0][2] == "Bearer tok" and b'"run_id"' in seen[0][3]
    assert [s[:2] for s in seen][-2:] == [("DELETE", "/sessions/s9"), ("DELETE", "/runs/r1/sessions")]

    def busy_handler(req):
        return httpx.Response(503, json={"error": "busy"})

    busy_http = httpx.AsyncClient(transport=httpx.MockTransport(busy_handler))
    busy = RemoteBrowser("http://browser", "tok", busy_http)
    with pytest.raises(BrowserBusy):
        await busy.open_session("r1", "m1")


async def test_rejected_action_detail_reaches_the_model():
    def handler(req):
        return httpx.Response(422, json={"detail": "click needs a ref"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ActionRejected, match="click needs a ref"):
        await RemoteBrowser("http://browser", "t", http).act("s", "click", {})

    class Rejecting(FakeBrowser):
        async def act(self, session_id, action, args):
            raise ActionRejected("scroll needs a direction")

    tool, _ = make(Rejecting())
    res = await tool.invoke({"action": "scroll"}, Ctx())
    assert res.is_error and "scroll needs a direction" in res.content
