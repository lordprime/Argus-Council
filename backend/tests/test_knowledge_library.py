"""Research library (auto-captured sources), text/URL documents and run-linked knowledge."""

import asyncio
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select

from argus.db.models import KnowledgeDocument
from argus.knowledge import service
from argus.providers.mock import MockProvider, council_responder
from argus.runtime.guardrails import GuardrailEngine, Guardrails
from argus.tools.web import WebFetch
from tests.test_data_retention import _uid
from tests.test_runs import events_of, wait_status

KB = "/api/v1/knowledge-bases"
PAGE = (
    "<title>Solid-state batteries</title><main><p>" + "Lithium metal anodes and sulfide electrolytes. " * 20
)


async def public(host):
    return ["93.184.216.34"]


def _client(body: str):
    return lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, html=body))
    )


async def _library(client, auth) -> dict:
    kbs = (await client.get(KB, headers=auth)).json()["knowledge_bases"]
    return next(k for k in kbs if k["kind"] == "library")


async def _docs(client, auth, kb_id) -> list[dict]:
    return (await client.get(f"{KB}/{kb_id}/documents", headers=auth)).json()["documents"]


async def test_web_fetch_captures_sources_into_the_library_once(app, client, auth):
    uid = await _uid(client, auth)

    async def emit(*a, **kw):
        return None

    ctx = SimpleNamespace(
        run=SimpleNamespace(
            engine=GuardrailEngine(Guardrails()),
            sources={},
            sink=SimpleNamespace(emit=emit),
            sessionmaker=app.state.db.sessionmaker,
            owner_id=uid,
            run_id=None,
        ),
        step_id="s1",
        member_name="R",
    )
    fetch = WebFetch(client_factory=_client(PAGE), resolver=public, capture=True)
    assert not (await fetch.invoke({"url": "https://ex.com/paper"}, ctx)).is_error
    await fetch.invoke({"url": "https://ex.com/paper"}, ctx)  # same page again: still one document

    lib = await _library(client, auth)
    docs = await _docs(client, auth, lib["id"])
    assert len(docs) == 1
    doc = docs[0]
    assert doc["origin"] == "agent" and doc["source_url"] == "https://ex.com/paper"
    assert doc["title"] == "Solid-state batteries"

    # Changed content replaces the stored copy; a capture-less fetcher stores nothing.
    changed = WebFetch(
        client_factory=_client(PAGE.replace("sulfide", "oxide")), resolver=public, capture=True
    )
    await changed.invoke({"url": "https://ex.com/paper"}, ctx)
    await WebFetch(client_factory=_client(PAGE), resolver=public).invoke({"url": "https://ex.com/other"}, ctx)
    docs = await _docs(client, auth, lib["id"])
    assert len(docs) == 1 and docs[0]["id"] != doc["id"]

    hits = (await client.get(f"{KB}/{lib['id']}/search", headers=auth, params={"q": "oxide"})).json()
    assert hits["results"][0]["source_url"] == "https://ex.com/paper"


async def test_library_evicts_least_recently_captured(app, client, auth, monkeypatch):
    uid = await _uid(client, auth)
    monkeypatch.setattr(service, "MAX_LIBRARY_CHUNKS", 2)
    text = "Enough words about one topic to be worth keeping in the library. " * 5
    async with app.state.db.sessionmaker() as s:
        for n in range(3):
            await service.capture_source(s, uid, url=f"https://ex.com/{n}", title=f"p{n}", text=text)
            await s.commit()
        lib = await service.library_kb(s, uid)
        urls = (
            await s.execute(select(KnowledgeDocument.source_url).where(KnowledgeDocument.kb_id == lib.id))
        ).scalars()
        assert sorted(urls) == ["https://ex.com/1", "https://ex.com/2"]
        # Too short to be worth keeping.
        assert await service.capture_source(s, uid, url="https://ex.com/x", title="", text="hi") is None


async def test_add_text_and_url_documents(client, auth, monkeypatch):
    kb = (await client.post(KB, headers=auth, json={"name": "papers"})).json()
    r = await client.post(
        f"{KB}/{kb['id']}/documents/text",
        headers=auth,
        json={
            "title": "Abstract: anodes",
            "text": "Anode dendrites short the cell.",
            "source_url": "https://x.org/a",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["origin"] == "text" and r.json()["filename"] == "Abstract- anodes.txt"

    import argus.api.v1.knowledge as api

    monkeypatch.setattr(api, "default_client", lambda allow_private: _client(PAGE)())
    monkeypatch.setattr(api, "default_resolver", public)
    r = await client.post(
        f"{KB}/{kb['id']}/documents/url", headers=auth, json={"url": "https://ex.com/paper"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["origin"] == "url" and r.json()["title"] == "Solid-state batteries"

    async def private(host):
        return ["10.0.0.5"]

    monkeypatch.setattr(api, "default_resolver", private)
    r = await client.post(f"{KB}/{kb['id']}/documents/url", headers=auth, json={"url": "https://in.ternal/x"})
    assert r.status_code == 400 and "private" in r.json()["error"]["message"]


class _Gated(MockProvider):
    """Holds the first execute-phase call until ``gate`` is set, so a test can act mid-run."""

    def __init__(self, gate: asyncio.Event, prompts: list[str]):
        super().__init__("gated", responder=self.reply)
        self.gate, self.prompts = gate, prompts

    def reply(self, req):
        if req.metadata.get("phase") == "execute":
            self.prompts.append(req.messages[0].content)
            if not any(m.role == "tool" for m in req.messages):  # one tool turn, so there is a next turn
                return {"content": "", "tool_calls": [{"name": "notes_read", "arguments": {}}]}
            return {"content": "done"}
        return council_responder(req)

    async def complete(self, request):
        if request.metadata.get("phase") == "execute":
            await self.gate.wait()
        return await super().complete(request)


async def test_link_knowledge_before_and_during_a_run(app, client, auth):
    first = (await client.post(KB, headers=auth, json={"name": "handbook", "description": "Policies"})).json()
    later = (await client.post(KB, headers=auth, json={"name": "late-notes"})).json()
    gate, prompts = asyncio.Event(), []
    app.state.llm.system["gated"] = _Gated(gate, prompts)

    bad = {
        "topic": "t",
        "mode": "chat",
        "provider": "gated",
        "model": "m",
        "knowledge_base_ids": [later["id"][:-1] + "0"],
    }
    assert (await client.post("/api/v1/runs", headers=auth, json=bad)).status_code == 404

    body = {
        "topic": "refunds",
        "mode": "chat",
        "size": 1,
        "provider": "gated",
        "model": "m",
        "knowledge_base_ids": [first["id"]],
    }
    r = await client.post("/api/v1/runs", headers=auth, json=body)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["knowledge_base_ids"] == [first["id"]]

    await asyncio.sleep(0.2)  # the run is now waiting inside its first execute call
    r = await client.post(
        f"/api/v1/runs/{rid}/knowledge", headers=auth, json={"knowledge_base_ids": [later["id"]]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["added"] == [later["id"]] and r.json()["live"] is True
    assert [k["name"] for k in r.json()["linked"]] == ["handbook", "late-notes"]
    gate.set()
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed", run

    # Agents were told about both, the late one from the turn after it was linked.
    assert prompts and "late-notes" in prompts[-1] and "handbook (0 documents): Policies" in prompts[-1]
    linked = [e for e in await events_of(client, auth, rid) if e["type"] == "knowledge.linked"]
    assert linked and linked[0]["data"]["names"] == ["late-notes"]

    r = await client.delete(f"/api/v1/runs/{rid}/knowledge/{first['id']}", headers=auth)
    assert [k["name"] for k in r.json()["linked"]] == ["late-notes"]
    assert (await client.get(f"/api/v1/runs/{rid}/knowledge", headers=auth)).json()["linked"][0][
        "name"
    ] == "late-notes"


async def test_linked_knowledge_widens_a_council_restricted_scope(app, client, auth):
    from argus.tools.knowledge import _scope

    uid = await _uid(client, auth)
    a = (await client.post(KB, headers=auth, json={"name": "a"})).json()
    b = (await client.post(KB, headers=auth, json={"name": "b"})).json()
    async with app.state.db.sessionmaker() as s:
        from argus.db.models import Run

        run = Run(
            owner_id=uid,
            topic="t",
            council_snapshot={"knowledge_base_ids": [a["id"]]},
            knowledge_base_ids=[b["id"]],
        )
        s.add(run)
        await service.library_kb(s, uid)
        await s.commit()
        n_docs = (await s.execute(select(func.count(KnowledgeDocument.id)))).scalar_one()
    ctx = SimpleNamespace(
        run=SimpleNamespace(extra={}, sessionmaker=app.state.db.sessionmaker, run_id=run.id, owner_id=uid)
    )
    scope = await _scope(ctx)
    assert set(scope["kbs"]) == {"a", "b", service.LIBRARY_NAME} and n_docs == 0
