"""L4: /snapshot reduces the event log page by page instead of loading up to 100k events at once."""

from __future__ import annotations

from argus.runtime.snapshot import SnapshotReducer, reduce_events
from tests.test_runs import API, events_of, wait_status


def _events() -> list[dict]:
    return [
        {"seq": 1, "type": "run.status", "data": {"status": "running"}},
        {"seq": 2, "type": "run.plan", "data": {"steps": [{"id": "s1", "title": "a"}]}},
        {"seq": 3, "type": "approval.requested", "data": {"approval_id": "a1"}},
        {"seq": 4, "type": "thought.completed", "data": {}},
        {"seq": 5, "type": "approval.resolved", "data": {"approval_id": "a1", "decision": "approve"}},
        {"seq": 6, "type": "run.status", "data": {"status": "completed"}},
    ]


def test_incremental_reducer_matches_one_shot():
    evs = _events()
    for at in (None, 0, 3, 5, 6):
        r = SnapshotReducer(at)
        for i in range(0, len(evs), 2):
            r.feed(evs[i : i + 2])
        assert r.result() == reduce_events(evs, at)
    assert reduce_events(evs, 3)["open_approvals"] == [{"approval_id": "a1", "status": "pending"}]


async def test_snapshot_endpoint_pages_through_events(app, client, auth, monkeypatch):
    import argus.api.v1.runs as runs_api

    run = (await client.post(API, headers=auth, json={"topic": "pages", "mode": "chat", "size": 2})).json()
    await wait_status(client, auth, run["id"])
    total = len(await events_of(client, auth, run["id"]))
    calls: list[int] = []
    real = runs_api.load_events

    async def spy(sessionmaker, run_id, after_seq=0, before_seq=None, limit=500):
        calls.append(limit)
        return await real(sessionmaker, run_id, after_seq, before_seq, limit)

    monkeypatch.setattr(runs_api, "load_events", spy)
    monkeypatch.setattr(runs_api, "SNAPSHOT_PAGE", 3)
    snap = (await client.get(f"{API}/{run['id']}/snapshot", headers=auth)).json()
    assert snap["status"] == "completed" and snap["seq"] == total
    assert calls and max(calls) == 3 and len(calls) >= total // 3
    # at_seq stops reading early.
    calls.clear()
    early = (await client.get(f"{API}/{run['id']}/snapshot", headers=auth, params={"at_seq": 1})).json()
    assert early["seq"] == 1 and early["status"] == "running"
    assert len(calls) == 1
