import asyncio
import io
import json
import zipfile

import pytest

from argus.providers.mock import MockProvider, council_responder
from argus.skills.package import SkillError, load_zip, parse_skill_md, validate_files

GOOD = """---
name: my-skill
description: Does a thing. Use when the thing is needed.
allowed-tools: notes_write, notes_read
---

# My skill
Step 1. Do it.
"""


def zip_of(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
    return buf.getvalue()


def test_parse_good():
    p = parse_skill_md(GOOD)
    assert p.name == "my-skill" and p.allowed_tools == ["notes_write", "notes_read"]
    assert p.body.startswith("# My skill")
    assert parse_skill_md(p.render()).description == p.description


@pytest.mark.parametrize(
    "text,msg",
    [
        ("no frontmatter", "frontmatter"),
        ("---\nname: Bad Name\ndescription: x\n---\nbody", "name"),
        ("---\nname: ok\n---\nbody", "description"),
        ("---\nname: ok\ndescription: d\n---\n", "instructions"),
        ("---\nname: ok\ndescription: " + "x" * 1025 + "\n---\nb", "description"),
        ("---\n[unclosed\n---\nb", "YAML"),
        ("---\n- a list\n---\nb", "mapping"),
    ],
)
def test_parse_errors(text, msg):
    with pytest.raises(SkillError, match=msg):
        parse_skill_md(text)


@pytest.mark.parametrize("path", ["../x.md", "/abs.md", ".hidden/x.md", "a/../../b.md", "evil.exe"])
def test_files_rejected(path):
    with pytest.raises(SkillError):
        validate_files({path: "x"})


def test_zip_roundtrip_and_folder_prefix():
    pkg = load_zip(zip_of({"my-skill/SKILL.md": GOOD, "my-skill/templates/t.md": "T"}))
    assert pkg.files == {"templates/t.md": "T"}
    assert load_zip(zip_of({"SKILL.md": GOOD})).name == "my-skill"


@pytest.mark.parametrize(
    "files,msg",
    [
        ({"README.md": "x"}, "SKILL.md"),
        ({"SKILL.md": GOOD, "../escape.md": "x"}, "Unsafe"),
        ({"SKILL.md": GOOD, "bin.md": b"\xff\xfe\x00"}, "UTF-8"),
        ({"a/SKILL.md": GOOD, "b/other.md": "x"}, "under the skill folder"),
        ({"SKILL.md": GOOD, "big.md": "x" * (5 * 1024 * 1024)}, "size limit"),
    ],
)
def test_zip_rejections(files, msg):
    with pytest.raises(SkillError, match=msg):
        load_zip(zip_of(files))


def test_not_a_zip():
    with pytest.raises(SkillError, match="zip"):
        load_zip(b"nope")


# ------------------------------------------------------------------ API
async def test_skill_api_crud(client, auth):
    listed = (await client.get("/api/v1/skills", headers=auth)).json()["skills"]
    names = {s["name"] for s in listed}
    assert {"research-report", "latex-paper", "code-review", "python-project"} <= names
    assert all(s["builtin"] for s in listed)

    r = await client.post("/api/v1/skills/validate", headers=auth, json={"skill_md": "bad"})
    assert r.json()["valid"] is False

    r = await client.post(
        "/api/v1/skills", headers=auth, json={"skill_md": GOOD, "files": {"refs/a.md": "A"}}
    )
    assert r.status_code == 201 and r.json()["files"] == ["refs/a.md"]
    assert (await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD})).status_code == 409

    got = (await client.get("/api/v1/skills/my-skill", headers=auth)).json()
    assert got["file_contents"] == {"refs/a.md": "A"} and got["skill_md"].startswith("---\nname: my-skill")

    upd = GOOD.replace("Step 1. Do it.", "Step 1. Do it better.")
    r = await client.put("/api/v1/skills/my-skill", headers=auth, json={"skill_md": upd})
    assert r.status_code == 200
    r = await client.put("/api/v1/skills/other", headers=auth, json={"skill_md": upd})
    assert r.status_code == 400  # name mismatch
    assert (await client.patch("/api/v1/skills/my-skill", headers=auth, json={"enabled": False})).json()[
        "enabled"
    ] is False

    exp = await client.get("/api/v1/skills/my-skill/export", headers=auth)
    assert exp.headers["content-type"] == "application/zip"
    assert (await client.delete("/api/v1/skills/my-skill", headers=auth)).status_code == 204
    r = await client.post(
        "/api/v1/skills/import", headers=auth, files={"file": ("s.zip", exp.content, "application/zip")}
    )
    assert r.status_code == 201 and r.json()["source"] == "upload"


async def test_builtins_read_only_and_shadowing(client, auth):
    assert (await client.delete("/api/v1/skills/code-review", headers=auth)).status_code == 403
    md = (await client.get("/api/v1/skills/code-review", headers=auth)).json()["skill_md"]
    assert (
        await client.put("/api/v1/skills/code-review", headers=auth, json={"skill_md": md})
    ).status_code == 403
    r = await client.post(
        "/api/v1/skills",
        headers=auth,
        json={"skill_md": md.replace("Review in this order", "House rules first")},
    )
    assert r.status_code == 201 and r.json()["shadows_builtin"] is True
    got = (await client.get("/api/v1/skills/code-review", headers=auth)).json()
    assert "House rules first" in got["skill_md"]


async def test_skills_private_per_user(client, auth):
    from tests.conftest import register_and_login

    await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD})
    other = await register_and_login(client, "z@example.com")
    assert (await client.get("/api/v1/skills/my-skill", headers=other)).status_code == 404


# ------------------------------------------------------------------ runtime
async def _run(client, auth, app, provider, guardrails=None):
    from tests.test_runs import events_of, wait_status

    r = await client.post(
        "/api/v1/runs",
        headers=auth,
        json={
            "topic": "t",
            "mode": "chat",
            "size": 2,
            "provider": provider,
            "model": "m",
            "guardrails": guardrails or {},
        },
    )
    run = await wait_status(client, auth, r.json()["id"])
    return run, await events_of(client, auth, run["id"])


async def test_agent_loads_skill_in_run(client, auth, app):
    await client.post(
        "/api/v1/skills", headers=auth, json={"skill_md": GOOD, "files": {"refs/a.md": "REF-A"}}
    )
    prompts, tool_results = [], []

    def responder(req):
        if req.metadata.get("phase") == "execute":
            prompts.append(req.messages[0].content)
            tool_msgs = [m for m in req.messages if m.role == "tool"]
            tool_results.extend(m.content for m in tool_msgs)
            if not tool_msgs:
                return {
                    "content": "",
                    "tool_calls": [{"name": "use_skill", "arguments": {"name": "my-skill"}}],
                }
            if len(tool_msgs) == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {"name": "skill_read", "arguments": {"name": "my-skill", "path": "refs/a.md"}}
                    ],
                }
            return {"content": "used the skill"}
        return council_responder(req)

    app.state.llm.system["sk"] = MockProvider("sk", responder=responder)
    run, evs = await _run(client, auth, app, "sk")
    assert run["status"] == "completed"
    assert "- my-skill: Does a thing." in prompts[0] and "- research-report:" in prompts[0]
    loaded = [e for e in evs if e["type"] == "skill.loaded"]
    assert loaded and loaded[0]["data"]["name"] == "my-skill"
    assert any("Step 1. Do it." in t for t in tool_results) and any("REF-A" in t for t in tool_results)


async def test_guardrails_hide_denied_skills(client, auth, app):
    prompts = []

    def responder(req):
        if req.metadata.get("phase") == "execute":
            prompts.append(req.messages[0].content)
            if not any(m.role == "tool" for m in req.messages):
                return {
                    "content": "",
                    "tool_calls": [{"name": "use_skill", "arguments": {"name": "latex-paper"}}],
                }
            return {"content": json.dumps([m.content for m in req.messages if m.role == "tool"])}
        return council_responder(req)

    app.state.llm.system["sk2"] = MockProvider("sk2", responder=responder)
    _, evs = await _run(client, auth, app, "sk2", {"skills": {"deny": ["latex-*"]}})
    assert "latex-paper" not in prompts[0] and "research-report" in prompts[0]
    assert not [e for e in evs if e["type"] == "skill.loaded"]
    done = [e for e in evs if e["type"] == "tool.call.completed"]
    assert done[0]["data"]["status"] == "error" and "No skill named" in done[0]["data"]["result_preview"]
    await asyncio.sleep(0)
