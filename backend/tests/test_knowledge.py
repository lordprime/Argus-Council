from argus.knowledge.bm25 import Doc, rank
from argus.knowledge.service import MAX_FILE_BYTES
from argus.knowledge.text import chunk_text, extract_text, tokenize
from argus.providers.mock import MockProvider, council_responder
from tests.conftest import register_and_login
from tests.test_runs import events_of, wait_status

KB = "/api/v1/knowledge-bases"


async def _kb(client, auth, name="docs"):
    r = await client.post(KB, headers=auth, json={"name": name, "description": "d"})
    assert r.status_code == 201, r.text
    return r.json()


async def _upload(client, auth, kb_id, name, body, ctype="text/markdown"):
    data = body.encode() if isinstance(body, str) else body
    return await client.post(f"{KB}/{kb_id}/documents", headers=auth, files={"file": (name, data, ctype)})


# ------------------------------------------------------------------ pure functions
def test_tokenize_and_chunking():
    assert tokenize("The Quick, brown FOX's 42 a") == ["quick", "brown", "fox", "42"]
    text = " ".join(f"word{i}" for i in range(600))
    chunks = chunk_text(text)
    assert len(chunks) > 1 and all(len(c) <= 800 for c in chunks)
    # Overlap: the start of each chunk repeats the tail of the previous one.
    assert chunks[1].split()[0] in chunks[0]
    assert chunk_text("   ") == []


def test_html_extraction_strips_tags_and_scripts():
    html = b"<html><head><script>evil()</script><style>x{}</style></head><body><h1>Title</h1><p>Hi</p></body>"
    out = extract_text(html, "text/html")
    assert "Title" in out and "Hi" in out and "evil" not in out and "<" not in out


def test_bm25_prefers_term_frequency_and_rarity():
    docs = [
        Doc("a", {"llama": 1, "farm": 5}, 6),
        Doc("b", {"llama": 4}, 4),
        Doc("c", {"farm": 1}, 1),
    ]
    assert [r for _, r in rank(["llama"], docs, 5)] == ["b", "a"]
    assert rank(["absent"], docs, 5) == []


# ------------------------------------------------------------------ API
async def test_upload_chunk_search_ranking(client, auth):
    kb = await _kb(client, auth)
    r = await _upload(client, auth, kb["id"], "cats.md", "# Cats\n\nCats purr. Cats sleep. Cats hunt mice.")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["chunk_count"] == 1 and doc["content_type"] == "text/markdown"
    await _upload(client, auth, kb["id"], "dogs.txt", "Dogs bark at cats sometimes.", "text/plain")
    long = "\n\n".join(f"Paragraph {i} about astronomy and telescopes." * 3 for i in range(60))
    r = await _upload(client, auth, kb["id"], "space.html", f"<p>{long}</p>", "text/html")
    assert r.status_code == 201 and r.json()["chunk_count"] > 3

    r = await client.get(f"{KB}/{kb['id']}/search", headers=auth, params={"q": "cats purr", "k": 5})
    body = r.json()
    assert body["terms"] == ["cats", "purr"]
    names = [x["filename"] for x in body["results"]]
    assert names[0] == "cats.md" and "dogs.txt" in names and "space.html" not in names
    top = body["results"][0]
    assert top["citation"] == "docs/cats.md#0" and top["chunk_index"] == 0 and top["score"] > 0

    lst = (await client.get(KB, headers=auth)).json()["knowledge_bases"]
    assert lst[0]["document_count"] == 3 and lst[0]["chunk_count"] > 5

    docs = (await client.get(f"{KB}/{kb['id']}/documents", headers=auth)).json()["documents"]
    cats = next(d for d in docs if d["filename"] == "cats.md")
    assert (await client.delete(f"{KB}/{kb['id']}/documents/{cats['id']}", headers=auth)).status_code == 204
    r = await client.get(f"{KB}/{kb['id']}/search", headers=auth, params={"q": "purr"})
    assert r.json()["results"] == []

    # Cross-KB search with a filter.
    other = await _kb(client, auth, "other")
    await _upload(client, auth, other["id"], "x.md", "telescopes everywhere")
    r = await client.get(f"{KB}/search", headers=auth, params={"q": "telescopes", "kb": other["id"]})
    assert {x["kb_name"] for x in r.json()["results"]} == {"other"}
    r = await client.get(f"{KB}/search", headers=auth, params={"q": "telescopes", "k": 50})
    assert {x["kb_name"] for x in r.json()["results"]} == {"docs", "other"}

    assert (await client.delete(f"{KB}/{kb['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"{KB}/{kb['id']}", headers=auth)).status_code == 404


async def test_kb_crud_and_conflicts(client, auth):
    kb = await _kb(client, auth, "one")
    assert (await client.post(KB, headers=auth, json={"name": "one"})).status_code == 409
    await _kb(client, auth, "two")
    r = await client.patch(f"{KB}/{kb['id']}", headers=auth, json={"name": "two"})
    assert r.status_code == 409
    r = await client.patch(f"{KB}/{kb['id']}", headers=auth, json={"name": "uno", "description": "x"})
    assert r.status_code == 200 and r.json()["name"] == "uno" and r.json()["description"] == "x"


async def test_size_and_type_limits(client, auth, monkeypatch):
    kb = await _kb(client, auth)
    r = await _upload(client, auth, kb["id"], "paper.pdf", b"%PDF-1.4 ...", "application/pdf")
    assert r.status_code == 415 and "PDF" in r.json()["error"]["message"]
    r = await _upload(client, auth, kb["id"], "x.exe", b"MZ\x00\x00", "application/octet-stream")
    assert r.status_code == 415
    r = await _upload(client, auth, kb["id"], "bin.txt", b"abc\x00def", "text/plain")
    assert r.status_code == 415
    r = await _upload(client, auth, kb["id"], "fake.md", "hello", "image/png")
    assert r.status_code == 415
    r = await _upload(client, auth, kb["id"], "big.txt", b"a " * (MAX_FILE_BYTES // 2 + 10), "text/plain")
    assert r.status_code == 413
    r = await _upload(client, auth, kb["id"], "empty.md", "   ")
    assert r.status_code == 400

    from argus.knowledge import service

    monkeypatch.setattr(service, "MAX_KB_CHUNKS", 3)
    text = " ".join(f"token{i}" for i in range(400))  # ~4 chunks
    r = await _upload(client, auth, kb["id"], "many.txt", text, "text/plain")
    assert r.status_code == 413 and "chunks" in r.json()["error"]["message"]


async def test_ownership_isolation(client, auth):
    kb = await _kb(client, auth)
    await _upload(client, auth, kb["id"], "secret.md", "classified launch codes")
    other = await register_and_login(client, "eve@example.com")
    assert (await client.get(f"{KB}/{kb['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{KB}/{kb['id']}/documents", headers=other)).status_code == 404
    r = await client.get(f"{KB}/{kb['id']}/search", headers=other, params={"q": "classified"})
    assert r.status_code == 404
    r = await _upload(client, other, kb["id"], "x.md", "pwn")
    assert r.status_code == 404
    assert (await client.delete(f"{KB}/{kb['id']}", headers=other)).status_code == 404
    assert (await client.get(KB, headers=other)).json()["knowledge_bases"] == []
    # Cross-KB search ignores ids the caller does not own.
    r = await client.get(f"{KB}/search", headers=other, params={"q": "classified", "kb": kb["id"]})
    assert r.json()["results"] == []
    mem = await client.post("/api/v1/memory", headers=auth, json={"text": "private"})
    assert mem.status_code == 201
    assert (await client.get("/api/v1/memory", headers=other)).json()["items"] == []
    assert (await client.delete(f"/api/v1/memory/{mem.json()['id']}", headers=other)).status_code == 404


async def test_api_key_needs_knowledge_scope(client, auth):
    r = await client.post(
        "/api/v1/auth/api-keys", headers=auth, json={"name": "ro", "scopes": ["runs:write"]}
    )
    assert r.status_code == 201, r.text
    key = {"X-API-Key": r.json()["key"]}
    assert (await client.post(KB, headers=key, json={"name": "k"})).status_code == 403
    assert (await client.get(KB, headers=key)).status_code == 200
    r = await client.post(
        "/api/v1/auth/api-keys", headers=auth, json={"name": "kw", "scopes": ["knowledge:write"]}
    )
    key = {"X-API-Key": r.json()["key"]}
    assert (await client.post(KB, headers=key, json={"name": "k"})).status_code == 201


# ------------------------------------------------------------------ agent tools
async def test_agent_uses_knowledge_and_memory_tools(client, auth, app):
    kb = await _kb(client, auth, "handbook")
    await _upload(client, auth, kb["id"], "policy.md", "Refunds are processed within 14 days of request.")
    await _upload(client, auth, kb["id"], "misc.md", "The office plant needs water weekly.")
    council = (
        await client.post(
            "/api/v1/councils",
            headers=auth,
            json={
                "name": "support",
                "size": 2,
                "mode": "chat",
                "default_provider": "kbm",
                "default_model": "m",
            },
        )
    ).json()
    tool_results: list[str] = []

    def responder(req):
        if req.metadata.get("phase") == "execute":
            tool_msgs = [m for m in req.messages if m.role == "tool"]
            if not tool_msgs:
                return {
                    "content": "",
                    "tool_calls": [{"name": "knowledge_search", "arguments": {"query": "refund days"}}],
                }
            if len(tool_msgs) == 1:
                tool_results.append(tool_msgs[0].content)
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "memory_write",
                            "arguments": {"key": "refunds", "text": "Refund SLA is 14 days", "tags": ["SLA"]},
                        }
                    ],
                }
            if len(tool_msgs) == 2:
                tool_results.append(tool_msgs[1].content)
                return {
                    "content": "",
                    "tool_calls": [{"name": "memory_search", "arguments": {"query": "refund"}}],
                }
            tool_results.append(tool_msgs[-1].content)
            return {"content": "Refunds take 14 days [Source 1]."}
        return council_responder(req)

    app.state.llm.system["kbm"] = MockProvider("kbm", responder=responder)
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={"topic": "refund policy", "mode": "chat", "council_id": council["id"]},
    )
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    assert "handbook/policy.md#0" in tool_results[0] and "14 days" in tool_results[0]
    assert "plant" not in tool_results[0]
    assert "Saved memory" in tool_results[1]
    assert "Refund SLA is 14 days" in tool_results[2]
    evs = await events_of(client, auth, run["id"])
    cites = [e for e in evs if e["type"] == "citation.added"]
    assert cites and cites[0]["data"]["url"] == "kb://handbook/policy.md#chunk-0"

    items = (await client.get("/api/v1/memory", headers=auth, params={"council_id": council["id"]})).json()
    assert len(items["items"]) == 1
    item = items["items"][0]
    assert item["key"] == "refunds" and item["tags"] == ["sla"] and item["run_id"] == run["id"]
    # Council-scoped: ad-hoc memory is separate.
    assert (await client.get("/api/v1/memory", headers=auth, params={"scope": "global"})).json()[
        "items"
    ] == []
    assert (await client.delete(f"/api/v1/memory/{item['id']}", headers=auth)).status_code == 204


async def test_knowledge_tools_respect_guardrail_deny(client, auth, app):
    await _kb(client, auth, "h")
    results = []

    def responder(req):
        if req.metadata.get("phase") == "execute":
            tool_msgs = [m for m in req.messages if m.role == "tool"]
            if not tool_msgs:
                return {
                    "content": "",
                    "tool_calls": [{"name": "knowledge_search", "arguments": {"query": "x"}}],
                }
            results.append(tool_msgs[0].content)
            return {"content": "done"}
        return council_responder(req)

    app.state.llm.system["kbd"] = MockProvider("kbd", responder=responder)
    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={
            "topic": "t",
            "mode": "chat",
            "size": 2,
            "provider": "kbd",
            "model": "m",
            "guardrails": {"tools": {"deny": ["knowledge_*"]}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    evs = await events_of(client, auth, run["id"])
    assert not [e for e in evs if e["type"] == "citation.added"]
    assert results and "denied" in results[0].lower()


async def test_multipart_uploads_get_the_larger_body_limit(client, auth):
    """Uploads get max_upload_bytes at the edge; ordinary JSON bodies stay at max_request_bytes."""
    kb = (await client.post("/api/v1/knowledge-bases", headers=auth, json={"name": "big"})).json()
    body = ("word " * 600_000).encode()  # ~3 MB: over the 2 MiB JSON limit, under the upload limit
    r = await client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        headers=auth,
        files={"file": ("big.txt", body, "text/plain")},
    )
    # It reaches the handler (which may still refuse it on its own chunk cap), not the edge limit.
    assert r.status_code == 201 or r.json()["error"]["code"] != "payload_too_large", r.text
    r = await client.post("/api/v1/runs", headers=auth, json={"topic": "t", "description": "x" * (3 << 20)})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
