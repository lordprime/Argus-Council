import pytest

from tests.conftest import register_and_login

API = "/api/v1/councils"


async def test_roles_catalog(client, auth):
    r = await client.get("/api/v1/roles", headers=auth)
    keys = {x["key"] for x in r.json()["roles"]}
    assert {"chair", "monitor", "security", "coder", "researcher"} <= keys


@pytest.mark.parametrize("mode", ["general", "research", "coding", "chat"])
async def test_create_default_roster(client, auth, mode):
    r = await client.post(API, json={"name": f"c-{mode}", "mode": mode, "size": 4}, headers=auth)
    assert r.status_code == 201, r.text
    members = r.json()["members"]
    assert len(members) == 4
    assert [m["position"] for m in members] == [0, 1, 2, 3]
    assert len({m["name"] for m in members}) == 4


async def test_create_explicit_members(client, auth):
    body = {
        "name": "custom",
        "members": [
            {"name": "Alpha", "role": "coder", "provider": "openai", "model": "gpt-x"},
            {"name": "Beta", "role": "critic"},
        ],
    }
    r = await client.post(API, json=body, headers=auth)
    assert r.status_code == 201
    assert [m["name"] for m in r.json()["members"]] == ["Alpha", "Beta"]


async def test_size_bounds_validated(client, auth):
    r = await client.post(API, json={"name": "x", "min_size": 3, "max_size": 4, "size": 6}, headers=auth)
    assert r.status_code == 422
    r = await client.post(API, json={"name": "x", "min_size": 5, "max_size": 4}, headers=auth)
    assert r.status_code == 422


async def test_duplicate_name(client, auth):
    assert (await client.post(API, json={"name": "dup"}, headers=auth)).status_code == 201
    assert (await client.post(API, json={"name": "dup"}, headers=auth)).status_code == 409


async def test_resize_up_and_down(client, auth):
    c = (await client.post(API, json={"name": "rs", "mode": "coding", "size": 3}, headers=auth)).json()
    r = await client.post(f"{API}/{c['id']}/resize", json={"size": 7}, headers=auth)
    assert r.status_code == 200
    roles = [m["role"] for m in r.json()["members"]]
    assert len(roles) == 7 and roles[:3] == ["chair", "architect", "coder"]
    assert "security" in roles and "monitor" in roles
    r = await client.post(f"{API}/{c['id']}/resize", json={"size": 2}, headers=auth)
    assert [m["role"] for m in r.json()["members"]] == ["chair", "architect"]
    # resize beyond max is a guardrail violation
    r = await client.post(f"{API}/{c['id']}/resize", json={"size": 13}, headers=auth)
    assert r.status_code == 422


async def test_resize_large_pads_workers(client, auth):
    c = (
        await client.post(API, json={"name": "big", "mode": "chat", "size": 2, "max_size": 20}, headers=auth)
    ).json()
    r = await client.post(f"{API}/{c['id']}/resize", json={"size": 9, "provider": "ollama"}, headers=auth)
    ms = r.json()["members"]
    assert len(ms) == 9
    assert all(m["provider"] == "ollama" for m in ms[2:])
    assert len({m["name"] for m in ms}) == 9


async def test_member_crud_and_bounds(client, auth):
    c = (
        await client.post(API, json={"name": "m", "size": 2, "min_size": 2, "max_size": 3}, headers=auth)
    ).json()
    cid = c["id"]
    r = await client.post(f"{API}/{cid}/members", json={"name": "Extra", "role": "critic"}, headers=auth)
    assert r.status_code == 201
    mid = r.json()["id"]
    r = await client.post(f"{API}/{cid}/members", json={"name": "Too many", "role": "critic"}, headers=auth)
    assert r.status_code == 422
    r = await client.patch(
        f"{API}/{cid}/members/{mid}", json={"position": 0, "temperature": 0.9}, headers=auth
    )
    assert r.json()["position"] == 0 and r.json()["temperature"] == 0.9
    order = [m["name"] for m in (await client.get(f"{API}/{cid}", headers=auth)).json()["members"]]
    assert order[0] == "Extra"
    assert (await client.delete(f"{API}/{cid}/members/{mid}", headers=auth)).status_code == 204
    first = (await client.get(f"{API}/{cid}", headers=auth)).json()["members"][0]["id"]
    assert (await client.delete(f"{API}/{cid}/members/{first}", headers=auth)).status_code == 422


async def test_patch_council_bounds(client, auth):
    c = (await client.post(API, json={"name": "p", "size": 4}, headers=auth)).json()
    r = await client.patch(f"{API}/{c['id']}", json={"max_size": 3}, headers=auth)
    assert r.status_code == 422
    r = await client.patch(
        f"{API}/{c['id']}", json={"description": "hi", "guardrails": {"max_steps": 5}}, headers=auth
    )
    assert r.status_code == 200 and r.json()["guardrails"] == {"max_steps": 5}


async def test_isolation_between_users(client, auth):
    c = (await client.post(API, json={"name": "mine"}, headers=auth)).json()
    other = await register_and_login(client, "other@example.com")
    assert (await client.get(f"{API}/{c['id']}", headers=other)).status_code == 404
    assert (await client.delete(f"{API}/{c['id']}", headers=other)).status_code == 404
    assert (await client.get(API, headers=other)).json() == []


async def test_invalid_member_role(client, auth):
    body = {"name": "bad", "members": [{"name": "X", "role": "Robert'); DROP TABLE"}]}
    assert (await client.post(API, json=body, headers=auth)).status_code == 422


async def test_delete_council(client, auth):
    c = (await client.post(API, json={"name": "del"}, headers=auth)).json()
    assert (await client.delete(f"{API}/{c['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"{API}/{c['id']}", headers=auth)).status_code == 404
