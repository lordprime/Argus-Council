from __future__ import annotations

import asyncio
import base64
import types
import uuid

import httpx
import pytest

from argus.browser.client import RemoteBrowser, SessionLost
from argus.browser.relay import FrameRelay
from argus.db.models import Run
from tests.conftest import register_and_login

API = "/api/v1/runs"


class FakeBrowser:
    def __init__(self, frames=(), *, hold=False):
        self.frames_list = list(frames)
        self.hold = hold
        self.calls = 0
        self.closed = 0

    async def frames(self, session_id, max_fps):
        self.calls += 1
        try:
            for f in self.frames_list:
                yield f
            if self.hold:
                await asyncio.Event().wait()
        finally:
            self.closed += 1


async def _run_row(app, client, auth) -> str:
    me = (await client.get("/api/v1/auth/me", headers=auth)).json()
    rid = uuid.uuid4()
    async with app.state.db.sessionmaker() as db:
        db.add(Run(id=rid, owner_id=uuid.UUID(me["id"]), topic="t", mode="chat", status="running"))
        await db.commit()
    return str(rid)


def _go_live(app, rid, sessions):
    ctx = types.SimpleNamespace(run_id=uuid.UUID(rid), extra={"browser_sessions": sessions})
    app.state.runs.live[uuid.UUID(rid)] = types.SimpleNamespace(
        ctx=ctx, task=None, finalising=True, cancel_timer=None
    )


async def _read(client, rid, auth, member="m1"):
    events = []
    async with client.stream("GET", f"{API}/{rid}/browser/{member}/frames", headers=auth) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line:
                events.append(line)
    return events


async def test_frames_require_auth_and_ownership(app, client, auth):
    rid = await _run_row(app, client, auth)
    _go_live(app, rid, {"m1": "s1"})
    assert (await client.get(f"{API}/{rid}/browser/m1/frames")).status_code == 401
    other = await register_and_login(client, "eve@example.com")
    assert (await client.get(f"{API}/{rid}/browser/m1/frames", headers=other)).status_code == 404


async def test_no_session_is_404(app, client, auth):
    rid = await _run_row(app, client, auth)
    # Not live in this process
    assert (await client.get(f"{API}/{rid}/browser/m1/frames", headers=auth)).status_code == 404
    _go_live(app, rid, {"m1": "s1"})
    app.state.frame_relay = FrameRelay(FakeBrowser(), 4)
    assert (await client.get(f"{API}/{rid}/browser/other/frames", headers=auth)).status_code == 404


async def test_relays_frames_for_the_member(app, client, auth):
    rid = await _run_row(app, client, auth)
    _go_live(app, rid, {"m1": "s1"})
    app.state.frame_relay = FrameRelay(FakeBrowser([b"a", b"b"]), 4, grace_s=0.05)
    events = await _read(client, rid, auth)
    assert events == [
        "event: frame",
        f"data: {base64.b64encode(b'a').decode()}",
        "event: frame",
        f"data: {base64.b64encode(b'b').decode()}",
        "event: end",
        "data: {}",
    ]


async def test_two_viewers_share_one_upstream():
    fake = FakeBrowser([b"x"], hold=True)
    relay = FrameRelay(fake, 4, grace_s=0.05)
    async with relay.watch("s1") as v1, relay.watch("s1") as v2:
        assert await v1.get(1) == b"x"
        assert await v2.get(1) == b"x"
        assert fake.calls == 1
    await asyncio.sleep(0.2)


async def test_upstream_stops_after_last_viewer_leaves():
    fake = FakeBrowser([b"x"], hold=True)
    relay = FrameRelay(fake, 4, grace_s=0.05)
    async with relay.watch("s1") as v:
        await v.get(1)
    assert fake.closed == 0  # still inside the grace period
    await asyncio.sleep(0.3)
    assert fake.closed == 1 and relay.active() == 0


async def test_slow_viewer_drops_frames_not_memory():
    fake = FakeBrowser([bytes([i]) for i in range(100)], hold=True)
    relay = FrameRelay(fake, 4, grace_s=0.05)
    async with relay.watch("s1") as v:
        await asyncio.sleep(0.1)  # never reads
        assert len(v.buffer) <= 2
        assert list(v.buffer) == [bytes([98]), bytes([99])]
    await asyncio.sleep(0.2)


class EndlessBrowser(FakeBrowser):
    async def frames(self, session_id, max_fps):
        self.calls += 1
        try:
            while True:
                yield b"f"
                await asyncio.sleep(0.01)
        finally:
            self.closed += 1


class FailingBrowser(FakeBrowser):
    async def frames(self, session_id, max_fps):
        self.calls += 1
        yield b"a"
        raise SessionLost()


async def test_hub_shutdown_ends_a_stream_that_is_receiving_frames(app, client, auth):
    rid = await _run_row(app, client, auth)
    _go_live(app, rid, {"m1": "s1"})
    app.state.frame_relay = FrameRelay(EndlessBrowser(), 4, grace_s=0.05)
    # The test transport returns only once the body is complete, so shutdown comes from a task while
    # frames are flowing; the stream must then end by itself. It waits for the stream to be open
    # first: a fixed timer raced the request on a slow CI runner and shut the hub down before it.
    hub = app.state.sse

    async def shut_down_once_streaming() -> None:
        for _ in range(400):  # up to 4 s for the stream to open
            if hub.buffers:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        hub.shutdown()

    stopper = asyncio.create_task(shut_down_once_streaming())
    events = await asyncio.wait_for(_read(client, rid, auth), 5)
    await stopper
    assert events.count("event: frame") >= 3
    assert events[-2:] == ["event: end", "data: {}"]


async def test_relay_shutdown_cancels_pumps_and_closes_upstreams():
    fake = EndlessBrowser()
    relay = FrameRelay(fake, 4, grace_s=60)
    async with relay.watch("s1") as v:
        assert await v.get(1) == b"f"
        await relay.shutdown()
        # viewers are released, the upstream iterator is closed
        while await v.get(1) is not None:
            pass
    assert fake.closed == 1 and relay.active() == 0


async def test_upstream_failure_ends_the_stream(app, client, auth):
    rid = await _run_row(app, client, auth)
    _go_live(app, rid, {"m1": "s1"})
    app.state.frame_relay = FrameRelay(FailingBrowser(), 4, grace_s=0.05)
    events = await asyncio.wait_for(_read(client, rid, auth), 5)
    assert events[-2:] == ["event: end", "data: {}"]


async def test_ticket_is_single_use_and_bound_to_its_run(app, client, auth):
    rid = await _run_row(app, client, auth)
    other_rid = await _run_row(app, client, auth)
    _go_live(app, rid, {"m1": "s1"})
    app.state.frame_relay = FrameRelay(FakeBrowser([b"a"]), 4, grace_s=0.05)
    url = f"{API}/{rid}/browser/m1/frames"
    t = (await client.post(f"{API}/{rid}/stream-ticket", headers=auth)).json()["ticket"]
    assert (await client.get(url, params={"ticket": t})).status_code == 200
    assert (await client.get(url, params={"ticket": t})).status_code == 401
    t2 = (await client.post(f"{API}/{other_rid}/stream-ticket", headers=auth)).json()["ticket"]
    assert (await client.get(url, params={"ticket": t2})).status_code == 401


async def test_client_ignores_keepalive_comments_and_404_is_session_lost():
    body = b": keepalive\n\ndata: " + base64.b64encode(b"jpg") + b"\n\n: keepalive\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if "gone" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    browser = RemoteBrowser("http://browser", "tok", client=http)
    assert [f async for f in browser.frames("s1", 4)] == [b"jpg"]
    with pytest.raises(SessionLost):
        [f async for f in browser.frames("gone", 4)]
