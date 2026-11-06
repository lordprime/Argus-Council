"""Tools recovering from the failures agents hit most: pages that are gone or refuse automated
readers (web_fetch), loosely named knowledge documents and workspace files, and the generic
recovery hints and retry in the agent loop. Also the protocol ledger's "http" entries."""

import io
import uuid
from types import SimpleNamespace

import httpx
import pytest

from argus.knowledge import service
from argus.providers.mock import council_responder
from argus.runtime import agent as agent_mod
from argus.runtime.context import Workspace
from argus.runtime.events import EventSink, MemoryBroker
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from argus.tools import web
from argus.tools.base import Tool, ToolRegistry, ToolResult
from argus.tools.builtin import ArtifactRead, builtin_tools, workspace_path
from argus.tools.knowledge import KnowledgeRead, KnowledgeSearch
from argus.tools.web import WebFetch
from tests.test_exports_and_tools import _Ctx, public
from tests.test_knowledge import _kb, _upload
from tests.test_runs import _tool_council, events_of, install, wait_status
from tests.test_runtime_core import db, run_row  # noqa: F401 - fixtures

GUESSED = "https://www.pewresearch.org/short-reads/2026/08/20/ai-bubble/"
ARTICLE = (
    "<html><head><title>AI spending, explained</title></head><body>"
    "<nav><a href='/a'>Home</a> <a href='/b'>Topics</a></nav><article><h1>AI spending</h1>"
    + "<p>Companies raised their spending on AI infrastructure again this quarter, analysts said. </p>"
    * 8
    + "<table><tr><th>Year</th><th>Capex</th></tr><tr><td>2024</td><td>200</td></tr>"
    "<tr><td>2025</td><td>310</td></tr></table></article><footer>Copyright</footer></body></html>"
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(web, "RETRY_BACKOFF_S", 0.0)


def _fetcher(handler, **kw):
    return WebFetch(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=public,
        **kw,
    )


def _wayback(snapshot: dict | None):
    return {"url": GUESSED, "archived_snapshots": {"closest": snapshot} if snapshot else {}}


# ------------------------------------------------------------------ web_fetch
async def test_404_falls_back_to_the_closest_archive_snapshot():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "www.pewresearch.org":
            return httpx.Response(404, html="<title>Not found</title>")
        if req.url.host == "archive.org":
            assert req.url.params["url"] == GUESSED
            return httpx.Response(
                200,
                json=_wayback(
                    {"available": True, "status": "200", "timestamp": "20250102030405", "url": "x"}
                ),
            )
        assert req.url.host == "web.archive.org" and "/web/20250102030405id_/" in req.url.path
        return httpx.Response(200, html=ARTICLE)

    ctx = _Ctx()
    res = await _fetcher(handler).invoke({"url": GUESSED}, ctx)
    assert not res.is_error, res.content
    archived = f"https://web.archive.org/web/20250102030405/{GUESSED}"
    assert res.content.startswith(f"[Source 1] AI spending, explained — {archived}")
    assert "Archived copy from the Internet Archive, captured 2025-01-02" in res.content
    assert GUESSED in res.content and "HTTP 404" in res.content
    assert res.data["archived"] is True and res.data["original_url"] == GUESSED
    assert res.data["url"] == archived and ctx.run.sources[archived]["n"] == 1
    # The live page is tried once (404 is not transient), then the archive's API and snapshot.
    assert [u.split("/")[2] for u in seen] == ["www.pewresearch.org", "archive.org", "web.archive.org"]


async def test_404_without_snapshot_tells_the_model_to_search():
    def handler(req):
        if req.url.host == "archive.org":
            return httpx.Response(200, json=_wayback(None))
        return httpx.Response(404, text="nope")

    res = await _fetcher(handler).invoke({"url": GUESSED}, _Ctx())
    assert res.is_error
    assert res.content.startswith("Fetch blocked: HTTP 404 from www.pewresearch.org")
    assert "(no archived copy either)" in res.content
    assert "use web_search to find the real address instead of constructing URLs" in res.content


async def test_archive_is_skipped_when_the_domain_policy_excludes_it():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        return httpx.Response(403, text="forbidden")

    rails = Guardrails.resolve({"network": {"allow_domains": ["*.example.org"]}})
    res = await _fetcher(handler).invoke({"url": "https://news.example.org/a"}, _Ctx(rails))
    assert res.is_error and hosts == ["news.example.org"]  # 403 is not retried, archive.org not asked
    assert "refuses automated readers" in res.content and "archived copy" not in res.content


async def test_transient_statuses_are_retried_with_browser_headers():
    calls = []

    def handler(req):
        calls.append(req.headers)
        if len(calls) < 3:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, text="finally")

    res = await _fetcher(handler).invoke({"url": "https://ex.com/slow"}, _Ctx())
    assert not res.is_error and "finally" in res.content and len(calls) == 3
    assert calls[0]["user-agent"].startswith("Mozilla/5.0") and "Chrome/" in calls[0]["user-agent"]
    assert "ArgusCouncil" not in calls[0]["user-agent"]
    assert "en-US" in calls[0]["accept-language"] and "text/html" in calls[0]["accept"]


async def test_bot_check_pages_are_reported_as_blocked():
    challenge = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"

    def handler(req):
        if req.url.host == "archive.org":
            return httpx.Response(200, json=_wayback(None))
        return httpx.Response(200, html=challenge)

    res = await _fetcher(handler).invoke({"url": "https://ex.com/a"}, _Ctx())
    assert res.is_error and "bot check" in res.content
    assert "try another source for the same information" in res.content.lower()


def _pdf(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 720, text)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


async def test_pdfs_are_read_as_text():
    body = _pdf("Quarterly GPU prices rose twelve percent")

    def handler(req):
        if req.url.path.endswith("generic.pdf"):  # object stores often send a generic binary type
            return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
        return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})

    for path in ("/report.pdf", "/files/generic.pdf"):
        res = await _fetcher(handler).invoke({"url": f"https://ex.com{path}"}, _Ctx())
        assert not res.is_error, res.content
        assert "Quarterly GPU prices rose twelve percent" in res.content
    res = await _fetcher(
        lambda r: httpx.Response(200, content=b"%PDF-1.4 broken", headers={"content-type": "application/pdf"})
    ).invoke({"url": "https://ex.com/bad.pdf"}, _Ctx())
    assert res.is_error and "PDF could not be read" in res.content


async def test_html_main_text_comes_from_trafilatura_with_tables():
    res = await _fetcher(lambda r: httpx.Response(200, html=ARTICLE)).invoke(
        {"url": "https://ex.com/a"}, _Ctx()
    )
    assert not res.is_error
    assert res.content.startswith("[Source 1] AI spending, explained — https://ex.com/a")
    assert "| 2025 | 310 |" in res.content  # tables kept, as rows
    assert "Copyright" not in res.content and "Topics" not in res.content
    # Nothing for trafilatura to extract: the plain fallback still returns the text.
    title, text = web.main_text("<title>T</title><p>Hi</p>")
    assert (title, text) == ("T", "Hi")


async def test_web_fetch_error_messages_are_actionable():
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    res = await _fetcher(timeout).invoke({"url": "https://ex.com/a"}, _Ctx())
    assert res.content.startswith("Fetch failed: ex.com did not respond in time")

    async def nxdomain(host):
        raise OSError("no such host")

    tool = WebFetch(client_factory=lambda: httpx.AsyncClient(), resolver=nxdomain)
    res = await tool.invoke({"url": "https://made-up.example/a"}, _Ctx())
    assert "Could not resolve made-up.example (DNS)" in res.content and "web_search" in res.content
    assert "never construct or guess" in WebFetch.description


# ------------------------------------------------------------------ knowledge_read
def test_document_refs_strip_kb_prefix_and_chunk_suffix():
    refs = service.document_refs(
        "Ai Bubble Data in market price/Price Comparision - Sheet1.pdf#3", ["Ai Bubble Data in market price"]
    )
    assert refs[-1] == "Price Comparision - Sheet1.pdf"
    assert service.document_refs("kb://docs/a/b.md#chunk-2", ["docs"])[-1] == "b.md"


def _kctx(app, kbs: dict[str, uuid.UUID]):
    async def emit(*a, **kw):
        return None

    return SimpleNamespace(
        run=SimpleNamespace(
            sessionmaker=app.state.db.sessionmaker,
            extra={"knowledge_scope": {"council_id": None, "kbs": kbs}},
            sources={},
            sink=SimpleNamespace(emit=emit),
        ),
        step_id="s1",
    )


async def test_knowledge_read_resolves_cited_names_and_suggests_files(client, auth, app):
    name = "Ai Bubble Data in market price"
    kb = await _kb(client, auth, name)
    for fname, body in (
        ("Price Comparision - Sheet1.csv", "Company,Price\nNvidia,180\n"),
        ("notes.md", "# N"),
    ):
        r = await _upload(client, auth, kb["id"], fname, body, "text/csv" if fname.endswith("csv") else None)
        assert r.status_code == 201, r.text
    ctx = _kctx(app, {name: uuid.UUID(kb["id"])})
    tool = KnowledgeRead()

    # The observed failure: the citation's "kb/file" form, with the knowledge base named as well.
    res = await tool.invoke(
        {"document": f"{name}/Price Comparision - Sheet1.csv", "knowledge_base": name}, ctx
    )
    assert not res.is_error, res.content
    assert "Nvidia" in res.content
    for ref in (
        f"{name}/Price Comparision - Sheet1.csv#2",  # with the chunk suffix
        "price comparision - sheet1.CSV",  # any case
        "Price Comparison - Sheet1.csv",  # a near miss (spelling corrected by the model)
    ):
        res = await tool.invoke({"document": ref, "knowledge_base": f"  {name.lower()} "}, ctx)
        assert not res.is_error and res.data["filename"] == "Price Comparision - Sheet1.csv", (
            ref,
            res.content,
        )

    res = await tool.invoke({"document": "Quarterly totals.xlsx"}, ctx)
    assert res.is_error and "Did you mean one of these?" in res.content
    assert f"- {name}/Price Comparision - Sheet1.csv" in res.content and f"- {name}/notes.md" in res.content

    # knowledge_search takes the knowledge base name loosely too.
    found = await KnowledgeSearch().invoke({"query": "Nvidia", "knowledge_bases": [name.upper()]}, ctx)
    assert not found.is_error, found.content


# ------------------------------------------------------------------ artifact_read
def test_workspace_path_normalisation():
    for raw in (
        "./report.md",
        "/report.md",
        "workspace/report.md",
        "/workspace/report.md",
        "/workspace/./report.md",
    ):
        assert workspace_path(raw) == "report.md", raw
    assert workspace_path("src/app.py") == "src/app.py"


async def test_artifact_read_tolerates_path_shapes_and_lists_files(db, run_row):  # noqa: F811
    eng = GuardrailEngine(Guardrails())
    ws = Workspace(
        run_row.id, db.sessionmaker, EventSink(run_row.id, None, db.sessionmaker, MemoryBroker()), eng
    )
    ctx = SimpleNamespace(run=SimpleNamespace(workspace=ws, engine=eng), member_name="W")
    tool = ArtifactRead()

    res = await tool.invoke({"path": "report.md"}, ctx)
    assert res.is_error and "workspace is empty" in res.content and "artifact_write" in res.content

    await ws.write("docs/Report.md", "# Findings", by="W")
    await ws.write("data.csv", "a,b", by="W")
    for path in ("workspace/docs/Report.md", "./docs/Report.md", "/workspace/docs/Report.md"):
        res = await tool.invoke({"path": path}, ctx)
        assert not res.is_error and res.content == "# Findings", path
    for path in ("docs/report.md", "Report.md", "docs/Reprot.md"):
        res = await tool.invoke({"path": path}, ctx)
        assert not res.is_error and res.content.endswith("# Findings"), path
        assert "closest match" in res.content and res.data["path"] == "docs/Report.md"
    res = await tool.invoke({"path": "summary.txt"}, ctx)
    assert res.is_error and "- data.csv" in res.content and "- docs/Report.md" in res.content


# ------------------------------------------------------------------ agent loop
class _Echo(Tool):
    name = "echo_tool"
    description = "Echo"

    from pydantic import BaseModel as _B

    class Args(_B):
        text: str
        times: int = 1

    async def run(self, args, ctx):
        return ToolResult(args.text)


def test_recovery_hints_are_specific():
    bad = ToolResult.error("Invalid arguments for echo_tool: text: Field required")
    agent_mod._with_hint(bad, agent_mod.classify_failure("error", bad.content), _Echo())
    assert "Recovery: Call echo_tool again" in bad.content
    assert "required: text (string)" in bad.content and "optional: times (integer)" in bad.content
    told = ToolResult.error("Fetch blocked: HTTP 404 ...; use web_search to find it")
    agent_mod._with_hint(told, "not_found", _Echo())
    assert "Recovery" not in told.content  # the tool already said what to do
    assert agent_mod.classify_failure("timeout", "x timed out") == "timeout"
    assert agent_mod.classify_failure("error", "No such file: a.md") == "not_found"
    assert agent_mod._transient(httpx.ReadTimeout("t")) and agent_mod._transient(TimeoutError())
    assert not agent_mod._transient(httpx.ConnectError("Destination x resolves to a disallowed address"))
    assert not agent_mod._transient(ValueError("bad"))


class _Flaky(Tool):
    name = "flaky_lookup"
    description = "Fails once with a dropped connection"
    calls = 0

    from pydantic import BaseModel as _B

    class Args(_B):
        q: str = ""

    async def run(self, args, ctx):
        type(self).calls += 1
        if type(self).calls == 1:
            raise httpx.ReadError("connection reset")
        return ToolResult("looked up")


async def test_run_ledgers_web_fetch_as_http_and_retries_transient_errors(client, auth, app):
    def handler(req):
        if req.url.host == "archive.org":
            return httpx.Response(
                200, json=_wayback({"available": True, "status": "200", "timestamp": "20250102", "url": "x"})
            )
        if req.url.host == "web.archive.org":
            return httpx.Response(200, html=ARTICLE)
        return httpx.Response(404, text="gone")

    _Flaky.calls = 0
    app.state.runs.tool_factory = lambda run: ToolRegistry([*builtin_tools(), _fetcher(handler), _Flaky()])

    def responder(req):
        if req.metadata.get("phase") == "execute":
            if any(m.role == "tool" for m in req.messages):
                return {"content": "done"}
            return {
                "content": "",
                "tool_calls": [
                    {"name": "web_fetch", "arguments": {"url": GUESSED}},
                    {"name": "flaky_lookup", "arguments": {}},
                    {"name": "notes_write", "arguments": {}},  # missing required fields
                ],
            }
        return council_responder(req)

    install(app, "webby", responder)
    cid = await _tool_council(client, auth, "webby", {"oversight": {"monitor_sensitivity": "off"}})
    r = await client.post("/api/v1/runs", headers=auth, json={"topic": "ai bubble", "council_id": cid})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed", run

    evs = await events_of(client, auth, rid)
    done = {e["data"]["tool_call_id"]: e["data"] for e in evs if e["type"] == "tool.call.completed"}
    started = {e["data"]["tool"]: e["data"]["tool_call_id"] for e in evs if e["type"] == "tool.call.started"}
    assert done[started["flaky_lookup"]]["status"] == "ok" and _Flaky.calls == 2  # retried once
    bad = done[started["notes_write"]]
    assert bad["status"] == "error" and "Recovery: Call notes_write again" in bad["result_preview"]
    assert "required: title (string), body (string)" in bad["result_preview"]

    calls = [e["data"] for e in evs if e["type"] == "protocol.call"]
    assert len(calls) == 1  # only the network tool is ledgered
    call = calls[0]
    assert call["protocol"] == "http" and call["direction"] == "outbound" and call["method"] == "web_fetch"
    assert call["peer"] == "www.pewresearch.org" and call["status"] == "ok"
    assert call["tool_call_id"] == started["web_fetch"]
    assert call["detail"]["archived"] is True and call["detail"]["purpose"] == "run"
    assert call["detail"]["url"].startswith("https://web.archive.org/web/20250102/")

    ledger = (await client.get("/api/v1/ledger", headers=auth, params={"protocol": "http"})).json()
    entry = next(e for e in ledger["entries"] if e["run_id"] == rid)
    assert entry["peer"] == "www.pewresearch.org" and entry["method"] == "web_fetch"
    assert entry["hash"] == call["hash"] and entry["status"] == "ok"
    assert (await client.get("/api/v1/ledger/verify", headers=auth)).json()["ok"] is True


async def test_ledger_failures_never_fail_the_tool_call(monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("argus.tools.remote.record_call", broken)
    runner = SimpleNamespace(member=SimpleNamespace(name="R"))
    tool = _fetcher(lambda r: httpx.Response(200, text="ok"))
    # Unbound call with a stand-in runner: only the ledger path is exercised.
    await agent_mod.AgentRunner._ledger_outbound(
        runner, tool, tool.risk, {"url": "https://ex.com"}, ToolResult("ok"), "ok", 0.0, None
    )


async def test_thin_pages_are_rendered_only_when_enabled(monkeypatch):
    shell = "<html><head><title>App</title></head><body><div id='root'></div><script>boot()</script></body></html>"
    rendered = []

    async def fake_render(url, **kw):
        rendered.append((url, kw["allow_domains"]))
        return url + "#!", ARTICLE

    monkeypatch.setattr(web, "render_page", fake_render)
    monkeypatch.setattr(web, "playwright_available", lambda: True)
    res = await _fetcher(lambda r: httpx.Response(200, html=shell)).invoke(
        {"url": "https://ex.com/app"}, _Ctx()
    )
    assert not res.is_error and rendered == []  # off by default
    res = await _fetcher(lambda r: httpx.Response(200, html=shell), render_js=True).invoke(
        {"url": "https://ex.com/app"}, _Ctx()
    )
    assert rendered == [("https://ex.com/app", ["*"])]
    assert "| 2025 | 310 |" in res.content and res.data["url"] == "https://ex.com/app#!"
    monkeypatch.setattr(web, "playwright_available", lambda: False)
    res = await _fetcher(lambda r: httpx.Response(200, html=shell), render_js=True).invoke(
        {"url": "https://ex.com/app"}, _Ctx()
    )
    assert len(rendered) == 1 and not res.is_error
