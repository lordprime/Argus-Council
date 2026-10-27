import io
import json
import zipfile

import httpx
import pytest

from argus.exporters import FORMATS, export, parse, to_txt
from argus.providers.mock import MockProvider, council_responder
from argus.runtime.guardrails import Guardrails
from argus.sandbox.executor import SandboxError, execute
from argus.tools.web import FetchBlocked, WebFetch, WebSearch, check_url, html_to_text
from tests.test_runs import events_of, wait_status

MD = """# Title & More

Para with **bold**, *it*, `code`, [ok](https://ex.com/a_b) [bad](javascript:alert(1)) <b>raw</b>.

- one
- two

| A | B |
|---|---|
| 1 | 2 |

```py
print(1)
```
"""


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_export_formats(fmt):
    data, mime, ext = export(MD, fmt, title="T & Co", metadata={"k": 1})
    assert data and mime == FORMATS[fmt][0] and ext == FORMATS[fmt][1]
    if fmt == "pdf":
        assert data.startswith(b"%PDF")
    elif fmt == "docx":
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert "word/document.xml" in names
    elif fmt == "html":
        text = data.decode()
        assert (
            "<b>raw</b>" not in text and "&lt;b&gt;raw&lt;/b&gt;" in text and 'href="javascript' not in text
        )
        assert "Content-Security-Policy" in text
    elif fmt == "latex":
        text = data.decode()
        assert r"\title{T \& Co}" in text and r"\textbf{bold}" in text and r"\begin{itemize}" in text
        assert r"\href{https://ex.com/a_b}" in text
    elif fmt == "json":
        doc = json.loads(data)
        assert doc["metadata"] == {"k": 1} and doc["sections"][0]["title"] == "Title & More"


def test_unknown_format():
    with pytest.raises(ValueError):
        export(MD, "exe", title="x")


def test_parse_and_txt():
    blocks = parse(MD)
    assert [b.kind for b in blocks] == ["heading", "para", "ul", "table", "code"]
    txt = to_txt(blocks)
    assert "Title & More\n============" in txt and "- one" in txt and "A | B" in txt


# ------------------------------------------------------------------ web tools
class _Ctx:
    """Minimal ToolCallContext stand-in for unit-testing tools."""

    def __init__(self, rails=None):
        from types import SimpleNamespace

        from argus.runtime.guardrails import GuardrailEngine

        self.events = []

        async def emit(type_, data, **kw):
            self.events.append((type_, data))

        self.run = SimpleNamespace(
            engine=GuardrailEngine(rails or Guardrails()), sources={}, sink=SimpleNamespace(emit=emit)
        )
        self.step_id = "s1"
        self.member_name = "R"


async def public(host):
    return ["93.184.216.34"]


async def private(host):
    return ["10.0.0.5"]


@pytest.mark.parametrize(
    "url,allow,resolver,msg",
    [
        ("ftp://x.com/a", ["*"], public, "http"),
        ("http://user:pw@x.com", ["*"], public, "credentials"),
        ("http://evil.com", ["*.wikipedia.org"], public, "not in the allowed"),
        ("http://internal.corp", ["*"], private, "private"),
    ],
)
async def test_check_url_blocks(url, allow, resolver, msg):
    with pytest.raises(FetchBlocked, match=msg):
        await check_url(url, allow, True, resolver)


async def test_check_url_allows_subdomain_and_apex():
    assert (
        await check_url("https://en.wikipedia.org/x", ["*.wikipedia.org"], True, public) == "en.wikipedia.org"
    )
    assert await check_url("https://wikipedia.org/x", ["*.wikipedia.org"], True, public) == "wikipedia.org"


async def test_ipv4_mapped_ipv6_blocked():
    async def mapped(host):
        return ["::ffff:127.0.0.1"]

    with pytest.raises(FetchBlocked):
        await check_url("http://x.com", ["*"], True, mapped)


def test_html_to_text():
    title, text = html_to_text(
        "<html><head><title>T</title><script>evil()</script></head>"
        "<body><nav>menu</nav><main><h1>Hi</h1><p>Body text</p></main></body></html>"
    )
    assert title == "T" and "Body text" in text and "evil" not in text and "menu" not in text


def _fetcher(handler, resolver=public):
    return WebFetch(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver
    )


async def test_web_fetch_records_citation_and_follows_safe_redirect():
    def handler(req):
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "/page"})
        return httpx.Response(200, html="<title>Doc</title><p>Hello world</p>")

    ctx = _Ctx()
    res = await _fetcher(handler).invoke({"url": "https://ex.com/start"}, ctx)
    assert not res.is_error and res.content.startswith("[Source 1] Doc — https://ex.com/page")
    assert ctx.events[0][0] == "citation.added" and ctx.events[0][1]["url"] == "https://ex.com/page"
    await _fetcher(handler).invoke({"url": "https://ex.com/page"}, ctx)
    assert len(ctx.run.sources) == 1  # same URL is one source


async def test_web_fetch_blocks_redirect_to_private():
    resolved = {"ex.com": ["93.184.216.34"], "metadata.internal": ["169.254.169.254"]}

    async def resolver(host):
        return resolved[host]

    def handler(req):
        return httpx.Response(302, headers={"location": "http://metadata.internal/latest"})

    res = await _fetcher(handler, resolver).invoke({"url": "https://ex.com/"}, _Ctx())
    assert res.is_error and "private" in res.content


async def test_web_fetch_rejects_binary_and_caps_size():
    res = await _fetcher(
        lambda r: httpx.Response(200, content=b"\x00", headers={"content-type": "image/png"})
    ).invoke({"url": "https://ex.com/x.png"}, _Ctx())
    assert res.is_error and "content type" in res.content
    big = "a" * 100_000
    res = await _fetcher(lambda r: httpx.Response(200, text=big)).invoke(
        {"url": "https://ex.com/big", "max_chars": 1000}, _Ctx()
    )
    assert res.content.endswith("[truncated]")


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("searxng", {"results": [{"title": "A", "url": "https://a", "content": "<b>s</b>nip"}]}),
        ("tavily", {"results": [{"title": "A", "url": "https://a", "content": "snip"}]}),
        ("brave", {"web": {"results": [{"title": "A", "url": "https://a", "description": "snip"}]}}),
    ],
)
async def test_web_search_providers(provider, payload):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        return httpx.Response(200, json=payload)

    tool = WebSearch(
        provider,
        api_key="k",
        base_url="http://searx:8080",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    res = await tool.invoke({"query": "solid state"}, _Ctx())
    assert "1. A\n   https://a\n   snip" in res.content
    if provider == "brave":
        assert seen["headers"]["x-subscription-token"] == "k"


# ------------------------------------------------------------------ sandbox executor
async def test_executor_runs_and_syncs_files():
    res = await execute(
        {"main.py": "print('hi')\nopen('out.txt','w').write('made')\n", "old.txt": "x"},
        "python3 main.py && rm old.txt",
        timeout_s=20,
    )
    assert res["exit_code"] == 0 and res["stdout"].strip() == "hi"
    assert res["changed"] == {"out.txt": "made"} and res["deleted"] == ["old.txt"]


async def test_executor_timeout_and_limits():
    res = await execute({}, "sleep 30", timeout_s=1)
    assert res["timed_out"] and res["exit_code"] == -9
    res = await execute({}, 'python3 -c "x = bytearray(3*1024*1024*1024)"', timeout_s=20, mem_mb=256)
    assert res["exit_code"] != 0


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../b"])
async def test_executor_rejects_unsafe_paths(path):
    with pytest.raises(SandboxError):
        await execute({path: "x"}, "true")


# ------------------------------------------------------------------ end to end
async def test_coding_run_with_shell_and_exports(client, auth, app, monkeypatch):
    from argus.sandbox.client import LocalSandbox
    from argus.tools.base import ToolRegistry
    from argus.tools.builtin import builtin_tools
    from argus.tools.code import code_tools

    app.state.runs.tool_factory = lambda run: ToolRegistry([*builtin_tools(), *code_tools(LocalSandbox())])

    def responder(req):
        meta = req.metadata
        if meta.get("phase") == "execute" and meta.get("role") == "coder":
            tools = [m for m in req.messages if m.role == "tool"]
            if not tools:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "fs_write",
                            "arguments": {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"},
                        }
                    ],
                }
            if len(tools) == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "fs_write",
                            "arguments": {
                                "path": "test_app.py",
                                "content": "from app import add\nassert add(2, 3) == 5\nprint('tests ok')\n",
                            },
                        }
                    ],
                }
            if len(tools) == 2:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "shell",
                            "arguments": {"command": "python3 test_app.py > result.txt && cat result.txt"},
                        }
                    ],
                }
            return {"content": "Implemented add() and verified: " + tools[-1].content[-200:]}
        return council_responder(req)

    app.state.llm.system["coder"] = MockProvider("coder", responder=responder)
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={
            "topic": "Add function",
            "mode": "coding",
            "size": 3,
            "provider": "coder",
            "model": "m",
            "outputs": ["md", "pdf", "html", "docx"],
            "guardrails": {"approval_required": []},
        },
    )
    run = await wait_status(client, auth, r.json()["id"], timeout=60)
    assert run["status"] == "completed", run
    evs = await events_of(client, auth, run["id"])
    shell_done = [e["data"] for e in evs if e["type"] == "tool.call.completed"][-1]
    assert shell_done["status"] == "ok" and "tests ok" in shell_done["result_preview"]
    arts = {
        a["path"]: a
        for a in (await client.get(f"/api/v1/runs/{run['id']}/artifacts", headers=auth)).json()["artifacts"]
    }
    assert {
        "app.py",
        "test_app.py",
        "result.txt",
        "SUMMARY.md",
        "exports/add-function.pdf",
        "exports/add-function.html",
        "exports/add-function.docx",
    } <= set(arts)
    assert {e["data"]["status"] for e in evs if e["type"] == "export.status"} == {"generating", "ready"}
    pdf = await client.get(
        f"/api/v1/runs/{run['id']}/artifacts/download",
        headers=auth,
        params={"path": "exports/add-function.pdf"},
    )
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert (
        pdf.headers["content-type"] == "application/pdf"
        and "attachment" in pdf.headers["content-disposition"]
    )
    txt = await client.get(
        f"/api/v1/runs/{run['id']}/artifacts/content",
        headers=auth,
        params={"path": "exports/add-function.pdf"},
    )
    assert txt.status_code == 400
    # on-demand export of a finished run
    r = await client.post(f"/api/v1/runs/{run['id']}/exports", headers=auth, json={"format": "latex"})
    assert r.status_code == 201 and r.json()["path"] == "exports/add-function.tex"
    tex = await client.get(
        f"/api/v1/runs/{run['id']}/artifacts/content",
        headers=auth,
        params={"path": "exports/add-function.tex"},
    )
    assert "\\begin{document}" in tex.text
    seqs = [e["seq"] for e in await events_of(client, auth, run["id"])]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_shell_requires_approval_by_default(client, auth, app):
    from argus.sandbox.client import LocalSandbox
    from argus.tools.base import ToolRegistry
    from argus.tools.code import code_tools
    from tests.test_runs import _wait_for_event

    app.state.runs.tool_factory = lambda run: ToolRegistry(code_tools(LocalSandbox()))

    def responder(req):
        if req.metadata.get("phase") == "execute" and req.metadata.get("role") == "coder":
            if not any(m.role == "tool" for m in req.messages):
                return {"content": "", "tool_calls": [{"name": "shell", "arguments": {"command": "echo hi"}}]}
            return {"content": "done"}
        return council_responder(req)

    app.state.llm.system["c2"] = MockProvider("c2", responder=responder)
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={"topic": "t", "mode": "coding", "size": 3, "provider": "c2", "model": "m"},
    )
    rid = r.json()["id"]
    req = await _wait_for_event(client, auth, rid, "approval.requested")
    assert req["data"]["risk"] == ["shell"]
    await client.post(
        f"/api/v1/runs/{rid}/approvals/{req['data']['approval_id']}",
        headers=auth,
        json={"decision": "reject", "reason": "no"},
    )
    run = await wait_status(client, auth, rid, timeout=30)
    assert run["status"] == "completed"


async def test_export_requires_deliverable(client, auth, app):
    import asyncio

    gate = asyncio.Event()

    class Slow(MockProvider):
        async def complete(self, request):
            await gate.wait()
            return self._build(request, council_responder(request))

    app.state.llm.system["slowx"] = Slow("slowx")
    rid = (
        await client.post(
            "/api/v1/runs",
            headers=auth,
            json={"topic": "t", "mode": "chat", "size": 2, "provider": "slowx", "model": "m"},
        )
    ).json()["id"]
    r = await client.post(f"/api/v1/runs/{rid}/exports", headers=auth, json={"format": "pdf"})
    assert r.status_code == 409
    gate.set()
    await wait_status(client, auth, rid)
