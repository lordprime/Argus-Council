"""The ledger purpose filter runs in the database, so it composes with the seq cursor and limit."""

import uuid

from argus.services import ledger


async def _seed(app, owner: uuid.UUID) -> None:
    run_id = uuid.uuid4()
    # seq: 1 discover, 2 run (explicit), 3 refresh, 4 run (implied by run_id), 5 untagged,
    # 6 discover, 7 run (implied), 8 discover
    plan = [
        ({"purpose": "discover"}, None),
        ({"purpose": "run"}, run_id),
        ({"purpose": "refresh"}, None),
        ({}, run_id),
        ({}, None),
        ({"purpose": "discover"}, None),
        ({"other": 1}, run_id),
        ({"purpose": "discover"}, None),
    ]
    for detail, rid in plan:
        await ledger.record(
            app.state.db.sessionmaker,
            owner_id=owner,
            protocol="mcp",
            direction="outbound",
            peer="https://mcp.example.com",
            method="tools/list",
            status="ok",
            request={},
            response={},
            run_id=rid,
            detail=detail,
        )


async def _page(client, auth, **params):
    r = await client.get("/api/v1/ledger", headers=auth, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_ledger_purpose_filter_is_server_side(client, auth, app):
    me = uuid.UUID((await client.get("/api/v1/auth/me", headers=auth)).json()["id"])
    await _seed(app, me)

    seqs = lambda body: [e["seq"] for e in body["entries"]]  # noqa: E731
    assert seqs(await _page(client, auth, purpose="discover")) == [8, 6, 1]
    assert seqs(await _page(client, auth, purpose="refresh")) == [3]
    assert seqs(await _page(client, auth, purpose="run")) == [7, 4, 2]

    # The filter applies before the limit, so a small page is still full of matches...
    first = await _page(client, auth, purpose="discover", limit=2)
    assert seqs(first) == [8, 6] and first["next_before_seq"] == 6
    # ...and the cursor continues within the filtered set.
    rest = await _page(client, auth, purpose="discover", limit=2, before_seq=first["next_before_seq"])
    assert seqs(rest) == [1] and rest["next_before_seq"] is None

    assert len((await _page(client, auth))["entries"]) == 8
    r = await client.get("/api/v1/ledger", headers=auth, params={"purpose": "bogus"})
    assert r.status_code == 422
