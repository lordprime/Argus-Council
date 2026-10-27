import pytest

from argus.skills.catalog import MARKETPLACE_DIR, marketplace_skills
from argus.skills.package import load_dir

GOOD = """---
name: my-skill
description: Does a thing. Use when the thing is needed.
allowed-tools: notes_write
---

# My skill
Step 1. Do it.
"""
V2 = GOOD.replace("Step 1. Do it.", "Step 1. Do it better.")


async def _versions(client, auth, name="my-skill"):
    r = await client.get(f"/api/v1/skills/{name}/versions", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def test_versions_created_on_create_and_update(client, auth):
    r = await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD, "files": {"a.md": "A"}})
    assert r.status_code == 201
    v = await _versions(client, auth)
    assert v["current"] == 1 and [x["number"] for x in v["versions"]] == [1]
    assert v["versions"][0]["author"] == "admin@example.com" and v["versions"][0]["note"] == "created"

    r = await client.put(
        "/api/v1/skills/my-skill", headers=auth, json={"skill_md": V2, "files": {"a.md": "B"}}
    )
    assert r.status_code == 200
    # Saving identical content does not create a new version.
    await client.put("/api/v1/skills/my-skill", headers=auth, json={"skill_md": V2, "files": {"a.md": "B"}})
    v = await _versions(client, auth)
    assert [x["number"] for x in v["versions"]] == [2, 1] and v["current"] == 2

    one = (await client.get("/api/v1/skills/my-skill/versions/1", headers=auth)).json()
    assert "Step 1. Do it." in one["skill_md"] and "better" not in one["skill_md"]
    assert one["file_contents"] == {"a.md": "A"} and one["created_at"]
    assert (await client.get("/api/v1/skills/my-skill/versions/9", headers=auth)).status_code == 404


async def test_restore_creates_new_version(client, auth):
    await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD, "files": {"a.md": "A"}})
    await client.put("/api/v1/skills/my-skill", headers=auth, json={"skill_md": V2})
    r = await client.post("/api/v1/skills/my-skill/versions/1/restore", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_from"] == 1 and body["current_version"] == 3 and body["files"] == ["a.md"]
    got = (await client.get("/api/v1/skills/my-skill", headers=auth)).json()
    assert "better" not in got["skill_md"] and got["file_contents"] == {"a.md": "A"}
    v = await _versions(client, auth)
    assert v["versions"][0]["note"] == "restored from v1"
    # Version 2 is still intact: history is immutable.
    two = (await client.get("/api/v1/skills/my-skill/versions/2", headers=auth)).json()
    assert "better" in two["skill_md"]


async def test_versions_private_and_builtin(client, auth):
    from tests.conftest import register_and_login

    await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD})
    other = await register_and_login(client, "v@example.com")
    assert (await client.get("/api/v1/skills/my-skill/versions", headers=other)).status_code == 404
    assert (await client.post("/api/v1/skills/my-skill/versions/1/restore", headers=other)).status_code == 404
    assert (await client.get("/api/v1/skills/code-review/versions", headers=auth)).status_code == 404


async def test_delete_removes_history(client, auth):
    await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD})
    await client.put("/api/v1/skills/my-skill", headers=auth, json={"skill_md": V2})
    assert (await client.delete("/api/v1/skills/my-skill", headers=auth)).status_code == 204
    await client.post("/api/v1/skills", headers=auth, json={"skill_md": GOOD})
    assert [x["number"] for x in (await _versions(client, auth))["versions"]] == [1]


# ------------------------------------------------------------------ marketplace
def test_catalogue_is_valid_and_curated():
    skills = marketplace_skills()
    assert 6 <= len(skills) <= 12
    dirs = sorted(d.name for d in MARKETPLACE_DIR.iterdir() if d.is_dir() and not d.name.startswith("_"))
    assert sorted(skills) == dirs  # every folder loads; none silently skipped
    for name, pkg in skills.items():
        assert pkg.allowed_tools and pkg.metadata.get("tags"), name
        assert load_dir(MARKETPLACE_DIR / name).name == name


async def test_marketplace_list_and_install(client, auth):
    r = await client.get("/api/v1/skills/marketplace", headers=auth)
    assert r.status_code == 200
    items = {s["name"]: s for s in r.json()["skills"]}
    assert "threat-model" in items and not items["threat-model"]["installed"]
    assert items["threat-model"]["tags"] and items["threat-model"]["version"] == "1.0.0"

    detail = (await client.get("/api/v1/skills/marketplace/threat-model", headers=auth)).json()
    assert detail["skill_md"].startswith("---\nname: threat-model") and detail["file_contents"]

    r = await client.post("/api/v1/skills/marketplace/threat-model/install", headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "marketplace" and r.json()["files"] == ["references/stride.md"]
    assert (
        await client.post("/api/v1/skills/marketplace/threat-model/install", headers=auth)
    ).status_code == 409

    items = {
        s["name"]: s for s in (await client.get("/api/v1/skills/marketplace", headers=auth)).json()["skills"]
    }
    assert items["threat-model"]["installed"] and items["threat-model"]["installed_version"] == "1.0.0"
    mine = {s["name"]: s for s in (await client.get("/api/v1/skills", headers=auth)).json()["skills"]}
    assert mine["threat-model"]["source"] == "marketplace"
    v = await _versions(client, auth, "threat-model")
    assert v["versions"][0]["note"].startswith("installed from marketplace")

    assert (await client.post("/api/v1/skills/marketplace/nope/install", headers=auth)).status_code == 404
    assert (await client.get("/api/v1/skills/marketplace/nope", headers=auth)).status_code == 404


async def test_marketplace_install_rejects_invalid_package(client, auth, monkeypatch):
    from argus.skills import catalog

    bad = catalog.marketplace_skills()["changelog-writer"]
    broken = type(bad)(
        name=bad.name,
        description=bad.description,
        body=bad.body,
        allowed_tools=bad.allowed_tools,
        files={"../escape.md": "x"},
    )
    monkeypatch.setattr("argus.api.v1.skills.marketplace_skills", lambda: {bad.name: broken})
    r = await client.post("/api/v1/skills/marketplace/changelog-writer/install", headers=auth)
    assert r.status_code == 400 and "Unsafe" in r.text
    assert (await client.get("/api/v1/skills/changelog-writer", headers=auth)).status_code == 404


@pytest.mark.parametrize(
    "path", ["/api/v1/skills/marketplace/adr-writer/install", "/api/v1/skills/x/versions/1/restore"]
)
async def test_viewer_cannot_install_or_restore(client, auth, path):
    from tests.conftest import register_and_login

    viewer = await register_and_login(client, "viewer@example.com")
    me = (await client.get("/api/v1/auth/me", headers=viewer)).json()
    r = await client.patch(f"/api/v1/admin/users/{me['id']}", headers=auth, json={"role": "viewer"})
    if r.status_code != 200:
        pytest.skip("role admin endpoint unavailable")
    assert (await client.post(path, headers=viewer)).status_code == 403
