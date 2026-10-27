import json
import time
import uuid

import httpx
import pytest

from argus.services.webhooks import WebhookDispatcher, sign, verify
from tests.test_runs import wait_status


def test_sign_and_verify():
    body = b'{"x":1}'
    header = sign("whsec_abc", body)
    assert verify("whsec_abc", body, header)
    assert not verify("whsec_other", body, header)
    assert not verify("whsec_abc", b'{"x":2}', header)
    old = sign("whsec_abc", body, int(time.time()) - 3600)
    assert not verify("whsec_abc", body, old)
    assert not verify("whsec_abc", body, "garbage")


def capture_dispatcher(app, statuses):
    """Swap the app's dispatcher HTTP client for a recording mock; returns the list of received requests."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        code = statuses.pop(0) if statuses else 200
        return httpx.Response(code)

    async def nosleep(_):
        pass

    d: WebhookDispatcher = app.state.webhooks
    d.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    d.sleep = nosleep
    return seen


async def test_webhook_api_and_lifecycle_delivery(client, auth, app):
    seen = capture_dispatcher(app, [])
    r = await client.post(
        "/api/v1/webhooks",
        headers=auth,
        json={
            "url": "https://hooks.example.com/argus",
            "events": ["run.completed", "run.started"],
            "description": "ci",
        },
    )
    assert r.status_code == 201
    secret = r.json()["secret"]
    assert secret.startswith("whsec_")
    listed = (await client.get("/api/v1/webhooks", headers=auth)).json()["webhooks"]
    assert "secret" not in listed[0] and "secret_enc" not in json.dumps(listed)

    run = (
        await client.post("/api/v1/runs", headers=auth, json={"topic": "hooked", "mode": "chat", "size": 2})
    ).json()
    await wait_status(client, auth, run["id"])
    # The API can report "completed" a moment before the run task queues its webhook; wait for the task.
    await app.state.runs.wait(uuid.UUID(run["id"]))
    await app.state.webhooks.drain()
    events = [req.headers["x-argus-event"] for req in seen]
    assert events.count("run.started") == 1 and events.count("run.completed") == 1
    req = next(r for r in seen if r.headers["x-argus-event"] == "run.completed")
    assert verify(secret, req.content, req.headers["x-argus-signature"])
    payload = json.loads(req.content)
    assert payload["data"]["run_id"] == run["id"] and payload["data"]["status"] == "completed"

    hook_id = r.json()["id"]
    dels = (await client.get(f"/api/v1/webhooks/{hook_id}/deliveries", headers=auth)).json()["deliveries"]
    assert len(dels) == 2 and all(d["ok"] for d in dels)
    rotated = (await client.post(f"/api/v1/webhooks/{hook_id}/rotate-secret", headers=auth)).json()["secret"]
    assert rotated != secret


async def test_webhook_retries_then_succeeds_and_4xx_stops(client, auth, app):
    seen = capture_dispatcher(app, [500, 502, 200])
    hook = (
        await client.post(
            "/api/v1/webhooks", headers=auth, json={"url": "https://h.example.com/x", "events": ["*"]}
        )
    ).json()
    r = await client.post(f"/api/v1/webhooks/{hook['id']}/test", headers=auth)
    assert r.json()["ok"] is True and len(seen) == 3
    assert len({req.headers["x-argus-delivery"] for req in seen}) == 1  # same delivery id across retries
    seen.clear()
    capture_dispatcher(app, [410])
    r = await client.post(f"/api/v1/webhooks/{hook['id']}/test", headers=auth)
    assert r.json()["ok"] is False and r.json()["failure_count"] == 1


@pytest.mark.parametrize(
    "body,code",
    [
        ({"url": "ftp://x.io", "events": ["run.completed"]}, 400),
        ({"url": "https://x.io", "events": ["nope"]}, 400),
        ({"url": "https://x.io", "events": []}, 422),
    ],
)
async def test_webhook_validation(client, auth, body, code):
    assert (await client.post("/api/v1/webhooks", headers=auth, json=body)).status_code == code


async def test_webhooks_private_per_user(client, auth):
    from tests.conftest import register_and_login

    hook = (
        await client.post("/api/v1/webhooks", headers=auth, json={"url": "https://x.io", "events": ["*"]})
    ).json()
    other = await register_and_login(client, "w@example.com")
    assert (await client.delete(f"/api/v1/webhooks/{hook['id']}", headers=other)).status_code == 404


async def test_idempotent_run_creation(client, auth):
    body = {"topic": "once", "mode": "chat", "size": 2}
    headers = {**auth, "Idempotency-Key": "abc-123"}
    a = await client.post("/api/v1/runs", headers=headers, json=body)
    b = await client.post("/api/v1/runs", headers=headers, json=body)
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] == b.json()["id"] and b.headers.get("idempotent-replayed") == "true"
    runs = (await client.get("/api/v1/runs", headers=auth)).json()
    assert len([r for r in runs if r["topic"] == "once"]) == 1
    c = await client.post("/api/v1/runs", headers=headers, json={**body, "topic": "different"})
    assert c.status_code == 409
    d = await client.post("/api/v1/runs", headers={**auth, "Idempotency-Key": "x" * 101}, json=body)
    assert d.status_code == 400
    await wait_status(client, auth, a.json()["id"])


async def test_runs_cursor_pagination(client, auth):
    ids = []
    for i in range(3):
        r = await client.post(
            "/api/v1/runs", headers=auth, json={"topic": f"p{i}", "mode": "chat", "size": 2}
        )
        ids.append(r.json()["id"])
        await wait_status(client, auth, r.json()["id"])
    page1 = (await client.get("/api/v1/runs", headers=auth, params={"limit": 2})).json()
    assert [r["topic"] for r in page1] == ["p2", "p1"]
    page2 = (
        await client.get("/api/v1/runs", headers=auth, params={"limit": 2, "before": page1[-1]["created_at"]})
    ).json()
    assert [r["topic"] for r in page2] == ["p0"]
