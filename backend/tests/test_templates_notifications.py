import asyncio

from tests.conftest import register_and_login
from tests.test_runs import _tool_council, _tool_run_setup, _wait_for_event, wait_status

T = "/api/v1/templates"
N = "/api/v1/notifications"


async def _notifications(client, auth, *, want=1, unread=None, timeout=10):
    params = {} if unread is None else {"unread": str(unread).lower()}
    for _ in range(int(timeout / 0.05)):
        body = (await client.get(N, headers=auth, params=params)).json()
        if len(body["notifications"]) >= want:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {want} notifications, got {body}")


async def test_template_crud_and_run_with_variables(client, auth):
    r = await client.post(
        T,
        headers=auth,
        json={
            "name": "Market scan",
            "summary": "Weekly scan",
            "topic": "Market for {{product}} in {{ region }}",
            "description": "Focus on {{product}} competitors",
            "mode": "research",
            "outputs": ["md"],
        },
    )
    assert r.status_code == 201, r.text
    tpl = r.json()
    assert tpl["variables"] == ["product", "region"] and tpl["use_count"] == 0

    dup = await client.post(T, headers=auth, json={"name": "Market scan", "topic": "x"})
    assert dup.status_code == 409

    assert [t["name"] for t in (await client.get(T, headers=auth)).json()["templates"]] == ["Market scan"]
    p = await client.patch(f"{T}/{tpl['id']}", headers=auth, json={"summary": "Monthly"})
    assert p.status_code == 200 and p.json()["summary"] == "Monthly"

    missing = await client.post(f"{T}/{tpl['id']}/run", headers=auth, json={"variables": {"product": "EVs"}})
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["missing"] == ["region"]

    run = await client.post(
        f"{T}/{tpl['id']}/run", headers=auth, json={"variables": {"product": "EVs", "region": "Europe"}}
    )
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["topic"] == "Market for EVs in Europe"
    assert body["description"] == "Focus on EVs competitors" and body["mode"] == "research"
    detail = (await client.get(f"{T}/{tpl['id']}", headers=auth)).json()
    assert detail["use_count"] == 1 and detail["last_used_at"]
    await wait_status(client, auth, body["id"])

    assert (await client.delete(f"{T}/{tpl['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"{T}/{tpl['id']}", headers=auth)).status_code == 404


async def test_template_validation(client, auth):
    bad = await client.post(T, headers=auth, json={"name": "g", "topic": "t", "guardrails": {"nope": 1}})
    assert bad.status_code == 422
    t = (await client.post(T, headers=auth, json={"name": "long", "topic": "{{x}}"})).json()
    too_long = await client.post(f"{T}/{t['id']}/run", headers=auth, json={"variables": {"x": "y" * 600}})
    assert too_long.status_code == 422


async def test_notification_on_run_completion_and_read(client, auth):
    tpl = (await client.post(T, headers=auth, json={"name": "plain", "topic": "Plain topic"})).json()
    run = (await client.post(f"{T}/{tpl['id']}/run", headers=auth, json={})).json()
    assert (await wait_status(client, auth, run["id"]))["status"] == "completed"
    body = await _notifications(client, auth)
    n = body["notifications"][0]
    assert n["kind"] == "run.completed" and n["run_id"] == run["id"] and n["read_at"] is None
    assert "Plain topic" in n["title"]
    assert (await client.get(f"{N}/unread-count", headers=auth)).json() == {"unread": 1}

    r = await client.post(f"{N}/{n['id']}/read", headers=auth)
    assert r.status_code == 200 and r.json()["read_at"]
    assert (await client.get(f"{N}/unread-count", headers=auth)).json()["unread"] == 0
    assert (await client.get(N, headers=auth, params={"unread": "true"})).json()["notifications"] == []


async def test_read_all_and_ownership_isolation(client, auth):
    for i in range(2):
        tpl = (await client.post(T, headers=auth, json={"name": f"t{i}", "topic": f"Topic {i}"})).json()
        run = (await client.post(f"{T}/{tpl['id']}/run", headers=auth, json={})).json()
        await wait_status(client, auth, run["id"])
    body = await _notifications(client, auth, want=2)
    assert body["unread"] == 2

    other = await register_and_login(client, "other@example.com")
    assert (await client.get(N, headers=other)).json() == {"notifications": [], "unread": 0}
    nid = body["notifications"][0]["id"]
    assert (await client.post(f"{N}/{nid}/read", headers=other)).status_code == 404
    tid = (await client.get(T, headers=auth)).json()["templates"][0]["id"]
    assert (await client.get(f"{T}/{tid}", headers=other)).status_code == 404
    assert (await client.post(f"{T}/{tid}/run", headers=other, json={})).status_code == 404
    assert (await client.get(T, headers=other)).json()["templates"] == []

    r = await client.post(f"{N}/read-all", headers=auth)
    assert r.status_code == 200 and r.json()["marked"] == 2
    assert (await client.get(f"{N}/unread-count", headers=auth)).json()["unread"] == 0


async def test_approval_requested_notification(client, auth, app):
    _tool_run_setup(app, "tooly-n")
    cid = await _tool_council(client, auth, "tooly-n", {"oversight": {"monitor_sensitivity": "off"}})
    rid = (
        await client.post("/api/v1/runs", headers=auth, json={"topic": "ship it", "council_id": cid})
    ).json()["id"]
    req = await _wait_for_event(client, auth, rid, "approval.requested")
    body = await _notifications(client, auth)
    n = body["notifications"][0]
    assert n["kind"] == "approval.requested" and n["run_id"] == rid
    assert n["data"]["approval_id"] == req["data"]["approval_id"]
    await client.post(
        f"/api/v1/runs/{rid}/approvals/{req['data']['approval_id']}",
        headers=auth,
        json={"decision": "reject"},
    )
    await wait_status(client, auth, rid)
