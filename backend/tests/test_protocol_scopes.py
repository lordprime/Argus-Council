"""A read-only API key must not start, steer or cancel runs through any agent protocol."""

from tests.test_protocols import rpc


async def _keys(client, auth):
    async def make(scopes):
        r = await client.post("/api/v1/auth/api-keys", headers=auth, json={"name": "k", "scopes": scopes})
        return r.json()["key"]

    return await make(["read"]), await make(["runs:write"])


async def test_read_only_key_cannot_write_over_protocols(client, auth):
    ro, rw = await _keys(client, auth)
    msg = {"message": {"role": "user", "parts": [{"kind": "text", "text": "topic"}], "messageId": "m1"}}

    r = (await rpc(client, ro, "message/send", msg, path="/a2a")).json()
    assert r["error"]["code"] == -32010
    task = (await rpc(client, rw, "message/send", msg, path="/a2a")).json()["result"]
    r = (await rpc(client, ro, "tasks/cancel", {"id": task["id"]}, path="/a2a")).json()
    assert r["error"]["code"] == -32010
    assert "result" in (await rpc(client, ro, "tasks/get", {"id": task["id"]}, path="/a2a")).json()

    r = await client.post(f"/acp/runs/{task['id']}/cancel", headers={"Authorization": f"Bearer {ro}"})
    assert r.status_code == 403

    for tool, args in (
        ("cancel_run", {"run_id": task["id"]}),
        ("intervene", {"run_id": task["id"], "kind": "pause"}),
    ):
        r = (await rpc(client, ro, "tools/call", {"name": tool, "arguments": args})).json()
        assert r["result"]["isError"] is True and "scope" in r["result"]["content"][0]["text"].lower()
