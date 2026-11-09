"""The browser service: read-only sessions behind a token, the engine's CLI/CDP/stream mechanics."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from argus.browser.engine import (
    ACTIONS,
    DOCUMENT_PATTERNS,
    FORBIDDEN_ACTIONS,
    AgentBrowserEngine,
    CdpGuard,
    CliReply,
    FakeEngine,
    SessionGone,
    _AbSession,
    action_policy_json,
    agent_browser_argv,
    build_command,
    chrome_argv,
    guard_commands,
    parse_cli_output,
    parse_stream_message,
    parse_tab_ids,
    relay_stream,
    wrap_boundaries,
)
from argus.browser.server import create_app
from argus.browser.sessions import ActionRefused, BrowserBusy, SessionManager, UnknownSession

TOKEN = "browser-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _setup(*, max_sessions=4, per_run=2, idle_timeout_s=300.0, wait_s=0.1, engine=None):
    engine = engine or FakeEngine()
    manager = SessionManager(
        engine, max_sessions=max_sessions, per_run=per_run, idle_timeout_s=idle_timeout_s, wait_s=wait_s
    )
    return engine, manager, create_app(token=TOKEN, manager=manager)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://browser")


async def _open(c, run_id="r1", member_id="m1"):
    return await c.post("/sessions", json={"run_id": run_id, "member_id": member_id}, headers=AUTH)


# --- routes -----------------------------------------------------------------------------------


async def test_requires_token():
    _, _, app = _setup()
    async with _client(app) as c:
        assert (await c.get("/health")).status_code == 200
        body = {"run_id": "r1", "member_id": "m1"}
        assert (await c.post("/sessions", json=body)).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert (await c.post("/sessions", json=body, headers=bad)).status_code == 401
        assert (await c.delete("/sessions/x", headers=bad)).status_code == 401
        assert (await c.post("/sessions/x/actions", json={"action": "snapshot"})).status_code == 401
        assert (await c.get("/sessions/x/frames")).status_code == 401
        assert (await c.delete("/runs/r1/sessions")).status_code == 401


async def test_no_configured_token_refuses_everything():
    manager = SessionManager(FakeEngine(), max_sessions=4, per_run=2, idle_timeout_s=300)
    app = create_app(token="", manager=manager)
    async with _client(app) as c:
        r = await c.post(
            "/sessions", json={"run_id": "r1", "member_id": "m1"}, headers={"Authorization": "Bearer "}
        )
        assert r.status_code == 401


async def test_open_act_close_round_trip():
    engine, _, app = _setup()
    async with _client(app) as c:
        r = await _open(c)
        assert r.status_code == 201
        sid = r.json()["session_id"]
        assert engine.started == [sid]

        r = await c.post(
            f"/sessions/{sid}/actions",
            json={"action": "open", "args": {"url": "https://example.com/a"}},
            headers=AUTH,
        )
        assert r.status_code == 200
        out = r.json()
        assert out["url"] == "https://example.com/a"
        assert out["title"] == "Page https://example.com/a"
        assert "AGENT_BROWSER_PAGE_CONTENT" in out["snapshot"]
        assert base64.b64decode(out["screenshot_b64"]) == FakeEngine.SCREENSHOT
        assert out["error"] is None
        assert engine.calls == [(sid, "open", {"url": "https://example.com/a"})]

        assert (await c.delete(f"/sessions/{sid}", headers=AUTH)).status_code == 204
        assert engine.stopped == [sid]
        r = await c.post(f"/sessions/{sid}/actions", json={"action": "snapshot"}, headers=AUTH)
        assert r.status_code == 404
        # Deleting again is harmless.
        assert (await c.delete(f"/sessions/{sid}", headers=AUTH)).status_code == 204


async def test_one_session_per_run_and_member():
    engine, _, app = _setup()
    async with _client(app) as c:
        a = (await _open(c, "r1", "m1")).json()["session_id"]
        b = (await _open(c, "r1", "m1")).json()["session_id"]
        assert a == b
        assert engine.started == [a]


@pytest.mark.parametrize("action", ["fill", "type", "press", "select", "upload", "download", "eval", "nope"])
async def test_forbidden_actions_are_refused(action):
    engine, _, app = _setup()
    async with _client(app) as c:
        sid = (await _open(c)).json()["session_id"]
        r = await c.post(
            f"/sessions/{sid}/actions", json={"action": action, "args": {"text": "x"}}, headers=AUTH
        )
        assert r.status_code == 422
        assert engine.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"url": "file:///etc/passwd"},
        {"url": "javascript:alert(1)"},
        {"url": "chrome://settings"},
        {"url": "data:text/html,hi"},
        {"url": "--disable-security"},
        {},
    ],
)
async def test_bad_open_args_are_refused(args):
    engine, _, app = _setup()
    async with _client(app) as c:
        sid = (await _open(c)).json()["session_id"]
        r = await c.post(f"/sessions/{sid}/actions", json={"action": "open", "args": args}, headers=AUTH)
        assert r.status_code == 422
        assert engine.calls == []


async def test_unknown_session_is_404():
    _, _, app = _setup()
    async with _client(app) as c:
        r = await c.post("/sessions/missing/actions", json={"action": "snapshot"}, headers=AUTH)
        assert r.status_code == 404
        assert (await c.get("/sessions/missing/frames", headers=AUTH)).status_code == 404


async def test_per_run_and_global_caps():
    engine, _, app = _setup(max_sessions=4, per_run=2, wait_s=0.1)
    async with _client(app) as c:
        assert (await _open(c, "r1", "m1")).status_code == 201
        assert (await _open(c, "r1", "m2")).status_code == 201
        t0 = time.monotonic()
        r = await _open(c, "r1", "m3")
        assert time.monotonic() - t0 >= 0.09  # waited for a slot before giving up
        assert r.status_code == 503
        assert r.json() == {"error": "busy"}

        assert (await _open(c, "r2", "m1")).status_code == 201
        assert (await _open(c, "r2", "m2")).status_code == 201
        r = await _open(c, "r3", "m1")  # four open across runs: the service is full
        assert r.status_code == 503
        assert len(engine.started) == 4


async def test_waiting_open_takes_a_freed_slot():
    engine, manager, _ = _setup(max_sessions=1, per_run=1, wait_s=2)
    first = await manager.open("r1", "m1")
    waiter = asyncio.create_task(manager.open("r2", "m1"))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await manager.close(first)
    second = await asyncio.wait_for(waiter, 1)
    assert second != first
    assert engine.started == [first, second]


async def test_close_run_frees_slots():
    engine, manager, app = _setup(max_sessions=4, per_run=2)
    async with _client(app) as c:
        a = (await _open(c, "r1", "m1")).json()["session_id"]
        b = (await _open(c, "r1", "m2")).json()["session_id"]
        other = (await _open(c, "r2", "m1")).json()["session_id"]
        r = await c.delete("/runs/r1/sessions", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"closed": 2}
        assert sorted(engine.stopped) == sorted([a, b])
        assert (await _open(c, "r1", "m3")).status_code == 201
        assert (await _open(c, "r1", "m4")).status_code == 201
        assert other in engine.sessions
        assert (await c.delete("/runs/r1/sessions", headers=AUTH)).json() == {"closed": 2}
    assert await manager.close_run("nobody") == 0


async def test_idle_sessions_are_reaped():
    engine, manager, app = _setup(idle_timeout_s=0.05)
    async with _client(app) as c:
        sid = (await _open(c)).json()["session_id"]
        await asyncio.sleep(0.1)
        assert await manager.reap_idle() == 1
        assert engine.stopped == [sid]
        r = await c.post(f"/sessions/{sid}/actions", json={"action": "snapshot"}, headers=AUTH)
        assert r.status_code == 404
        assert await manager.reap_idle() == 0


async def test_busy_session_is_not_reaped():
    engine = FakeEngine(act_delay_s=0.2)
    _, manager, _ = _setup(idle_timeout_s=0.05, engine=engine)
    sid = await manager.open("r1", "m1")
    await asyncio.sleep(0.1)
    act = asyncio.create_task(manager.act(sid, "snapshot", {}))
    await asyncio.sleep(0.05)
    assert await manager.reap_idle() == 0
    await act
    assert await manager.reap_idle() == 0  # just used


async def test_lost_engine_session_becomes_unknown():
    engine, manager, _ = _setup()
    sid = await manager.open("r1", "m1")
    engine.sessions.pop(sid)  # the browser died under us
    with pytest.raises(UnknownSession):
        await manager.act(sid, "snapshot", {})
    with pytest.raises(UnknownSession):
        await manager.act(sid, "snapshot", {})
    # Its slot is free again.
    assert await manager.open("r1", "m1") != sid


async def test_manager_raises_typed_errors():
    _, manager, _ = _setup(max_sessions=1, per_run=1, wait_s=0.01)
    sid = await manager.open("r1", "m1")
    with pytest.raises(BrowserBusy):
        await manager.open("r1", "m2")
    with pytest.raises(ActionRefused):
        await manager.act(sid, "eval", {"script": "1"})
    with pytest.raises(UnknownSession):
        await manager.act("nope", "snapshot", {})
    with pytest.raises(UnknownSession):
        manager.frames("nope", 4)


async def test_frames_stream_relays_engine_frames():
    frames = [b"\xff\xd8one", b"\xff\xd8two", b"\xff\xd8three"]
    engine, _, app = _setup(engine=FakeEngine(frames=frames))
    async with _client(app) as c:
        sid = (await _open(c)).json()["session_id"]
        r = await c.get(f"/sessions/{sid}/frames?max_fps=4", headers=AUTH)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        data = [line[len("data: ") :] for line in r.text.splitlines() if line.startswith("data: ")]
        assert data == [base64.b64encode(f).decode() for f in frames]
    assert engine.frame_requests == [(sid, 4)]
    assert engine.streams_closed == 1


async def _drive_frames(app, path: str, *, spec_version: str) -> list[bytes]:
    """Call the ASGI app by hand and disconnect after the first frame."""
    bodies: list[bytes] = []
    got_frame = asyncio.Event()

    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await got_frame.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            if got_frame.is_set() and spec_version >= "2.4":
                raise OSError("client went away")
            if message.get("body", b"").startswith(b"data: "):
                bodies.append(message["body"])
                got_frame.set()

    path, _, query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode()), (b"host", b"browser")],
        "client": ("127.0.0.1", 1),
        "server": ("browser", 80),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), 5)
    except Exception as exc:  # Starlette reports the 2.4-style disconnect as ClientDisconnect
        assert type(exc).__name__ == "ClientDisconnect", exc
    return bodies


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_frames_stop_when_the_client_disconnects(spec_version):
    engine = FakeEngine(endless=True, frame_interval_s=0.01)
    _, manager, app = _setup(engine=engine)
    sid = await manager.open("r1", "m1")
    bodies = await _drive_frames(app, f"/sessions/{sid}/frames?max_fps=2", spec_version=spec_version)
    assert bodies
    for _ in range(50):
        if engine.streams_closed:
            break
        await asyncio.sleep(0.01)
    assert engine.streams_open == 1
    assert engine.streams_closed == 1


# --- pure engine mechanics ----------------------------------------------------------------------


def test_actions_and_forbidden_lists():
    assert ACTIONS == ("open", "snapshot", "click", "scroll", "get_text", "back", "forward", "wait")
    assert set(FORBIDDEN_ACTIONS) == {"fill", "type", "press", "select", "upload", "download", "eval"}


def test_action_policy_denies_every_write_action():
    policy = json.loads(action_policy_json())
    assert policy["default"] == "allow"
    for name in ("fill", "type", "press", "select", "upload", "download", "eval"):
        assert name in policy["deny"]
    for name in ("navigate", "click", "scroll", "snapshot", "get", "wait"):
        assert name not in policy["deny"]


def test_chrome_argv_confines_the_network():
    argv = chrome_argv(
        "/opt/chrome/chrome",
        port=9333,
        proxy="http://egress:3128",
        user_data_dir="/tmp/argus-browser-x/profile",
        viewport=(1280, 800),
        extra=("--no-sandbox", "--proxy-server=http://evil:1"),
    )
    assert argv[0] == "/opt/chrome/chrome"
    for flag in (
        "--headless=new",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9333",
        "--proxy-server=http://egress:3128",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-background-networking",
        "--user-data-dir=/tmp/argus-browser-x/profile",
        "--window-size=1280,800",
        "--no-sandbox",
    ):
        assert flag in argv
    # Operator extras never override the confinement flags: Chrome takes the last copy of a switch.
    proxies = [a for a in argv if a.startswith("--proxy-server=")]
    assert proxies[-1] == "--proxy-server=http://egress:3128"
    assert argv[-1] == "about:blank"


def test_agent_browser_argv_attaches_with_the_safety_flags():
    argv = agent_browser_argv(
        "agent-browser", cdp_port=9333, session="s1", policy_path="/tmp/p.json", command=["back"]
    )
    assert argv == [
        "agent-browser",
        "--cdp",
        "9333",
        "--session",
        "s1",
        "--json",
        "--action-policy",
        "/tmp/p.json",
        "--content-boundaries",
        "--max-output",
        "30000",
        "back",
    ]
    assert "--allowed-domains" not in argv


@pytest.mark.parametrize(
    ("action", "args", "command"),
    [
        ("open", {"url": "https://example.com/a?b=1"}, ["open", "https://example.com/a?b=1"]),
        ("open", {"url": "http://example.com"}, ["open", "http://example.com"]),
        ("snapshot", {}, ["snapshot", "-i"]),
        ("snapshot", {"full": True}, ["snapshot"]),
        ("click", {"ref": "e3"}, ["click", "@e3"]),
        ("click", {"ref": "@e12", "url": None}, ["click", "@e12"]),
        ("scroll", {}, ["scroll", "down", "600"]),
        ("scroll", {"direction": "up"}, ["scroll", "up", "600"]),
        ("scroll", {"direction": "down", "amount": 1200}, ["scroll", "down", "1200"]),
        ("get_text", {}, ["get", "text", "body"]),
        ("get_text", {"ref": "@e2"}, ["get", "text", "@e2"]),
        ("back", {}, ["back"]),
        ("forward", {}, ["forward"]),
        ("wait", {}, ["wait", "1000"]),
        ("wait", {"ms": 500}, ["wait", "500"]),
        ("wait", {"ref": "e4"}, ["wait", "@e4"]),
    ],
)
def test_build_command(action, args, command):
    assert build_command(action, args) == command


@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("fill", {"ref": "@e1", "text": "x"}),
        ("eval", {}),
        ("open", {}),
        ("open", {"url": "ftp://example.com"}),
        ("open", {"url": "https://exa mple.com"}),
        ("open", {"url": "https:///nohost"}),
        ("open", {"url": "https://example.com/" + "a" * 2100}),
        ("click", {}),
        ("click", {"ref": "--help"}),
        ("click", {"ref": "body"}),
        ("scroll", {"direction": "sideways"}),
        ("scroll", {"amount": -5}),
        ("scroll", {"amount": "10; rm"}),
        ("wait", {"ms": 99_999}),
        ("wait", {"ms": True}),
        ("get_text", {"ref": "#main"}),
    ],
)
def test_build_command_refuses(action, args):
    with pytest.raises(ValueError):
        build_command(action, args)


def test_guard_blocks_non_get_documents():
    attached = {
        "method": "Target.attachedToTarget",
        "sessionId": None,
        "params": {"sessionId": "S1", "targetInfo": {"type": "page"}, "waitingForDebugger": True},
    }
    cmds = guard_commands(attached)
    assert [c[0] for c in cmds] == ["Fetch.enable", "Target.setAutoAttach", "Runtime.runIfWaitingForDebugger"]
    assert cmds[0][1] == {"patterns": DOCUMENT_PATTERNS}
    assert DOCUMENT_PATTERNS == [{"resourceType": "Document", "requestStage": "Request"}]
    assert cmds[1][1] == {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
    assert all(c[2] == "S1" for c in cmds)

    worker = {
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "W", "targetInfo": {"type": "worker"}},
    }
    assert guard_commands(worker) == [("Runtime.runIfWaitingForDebugger", {}, "W")]

    def paused(method):
        return {
            "method": "Fetch.requestPaused",
            "sessionId": "S1",
            "params": {
                "requestId": "R9",
                "request": {"method": method, "url": "https://x"},
                "resourceType": "Document",
            },
        }

    assert guard_commands(paused("POST")) == [
        ("Fetch.failRequest", {"requestId": "R9", "errorReason": "BlockedByClient"}, "S1")
    ]
    assert guard_commands(paused("put"))[0][0] == "Fetch.failRequest"
    assert guard_commands(paused("GET")) == [("Fetch.continueRequest", {"requestId": "R9"}, "S1")]
    assert guard_commands(paused("HEAD"))[0][0] == "Fetch.continueRequest"
    assert guard_commands({"method": "Page.loadEventFired", "params": {}}) == []
    assert guard_commands({"id": 3, "result": {}}) == []


async def test_cdp_guard_against_a_fake_chrome():
    """The guard enables interception before resuming a new page, and fails POST documents."""
    received: list[dict] = []
    done = asyncio.Event()

    async def chrome(ws):
        async for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "result": {},
                        **({"sessionId": msg["sessionId"]} if msg.get("sessionId") else {}),
                    }
                )
            )
            if msg["method"] == "Target.setAutoAttach" and "sessionId" not in msg:
                await ws.send(
                    json.dumps(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {
                                "sessionId": "S1",
                                "targetInfo": {"type": "page"},
                                "waitingForDebugger": True,
                            },
                        }
                    )
                )
            if msg["method"] == "Runtime.runIfWaitingForDebugger":
                for rid, method in (("R1", "POST"), ("R2", "GET")):
                    await ws.send(
                        json.dumps(
                            {
                                "method": "Fetch.requestPaused",
                                "sessionId": "S1",
                                "params": {"requestId": rid, "request": {"method": method}},
                            }
                        )
                    )
            if msg["method"] == "Fetch.continueRequest":
                done.set()

    async with serve(chrome, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        guard = CdpGuard(f"ws://127.0.0.1:{port}/devtools/browser/x")
        await guard.start()
        assert guard.alive
        await asyncio.wait_for(done.wait(), 3)
        await guard.close()
        assert not guard.alive

    methods = [(m["method"], m.get("sessionId")) for m in received]
    assert methods[0] == ("Target.setAutoAttach", None)
    assert received[0]["params"] == {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
    order = [m for m, s in methods if s == "S1"]
    assert order.index("Fetch.enable") < order.index("Runtime.runIfWaitingForDebugger")
    fails = [m for m in received if m["method"] == "Fetch.failRequest"]
    assert [f["params"] for f in fails] == [{"requestId": "R1", "errorReason": "BlockedByClient"}]
    conts = [m for m in received if m["method"] == "Fetch.continueRequest"]
    assert [c["params"] for c in conts] == [{"requestId": "R2"}]


async def test_cdp_guard_reports_a_dropped_connection():
    async def chrome(ws):
        await ws.recv()
        await ws.close()

    async with serve(chrome, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        guard = CdpGuard(f"ws://127.0.0.1:{port}/")
        with pytest.raises(Exception):  # noqa: B017 - the connection closes before the reply
            await guard.start()
        for _ in range(50):
            if not guard.alive:
                break
            await asyncio.sleep(0.01)
        assert not guard.alive
        await guard.close()


def test_parse_stream_message():
    jpeg = b"\xff\xd8\xff\xe0frame"
    msg = json.dumps({"type": "frame", "seq": 7, "data": base64.b64encode(jpeg).decode(), "metadata": {}})
    assert parse_stream_message(msg) == (jpeg, 7)
    assert parse_stream_message(json.dumps({"type": "frame", "data": base64.b64encode(jpeg).decode()})) == (
        jpeg,
        None,
    )
    assert parse_stream_message(json.dumps({"type": "status", "connected": True})) is None
    assert parse_stream_message(json.dumps({"type": "frame", "data": "!!not base64!!"})) is None
    assert parse_stream_message("not json") is None
    assert parse_stream_message(b"\x00") is None


async def test_relay_stream_paces_with_acks_and_never_sends_input():
    jpegs = [b"\xff\xd8a", b"\xff\xd8b", b"\xff\xd8c"]
    from_client: list[dict] = []
    closed = asyncio.Event()

    async def stream_server(ws):
        try:
            from_client.append(json.loads(await ws.recv()))  # config
            for seq, jpeg in enumerate(jpegs, start=1):
                await ws.send(json.dumps({"type": "status", "connected": True}))
                await ws.send(
                    json.dumps({"type": "frame", "seq": seq, "data": base64.b64encode(jpeg).decode()})
                )
                from_client.append(json.loads(await ws.recv()))  # ack
        except ConnectionClosed:
            pass
        finally:
            closed.set()

    async with serve(stream_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        got = []
        stream = relay_stream(f"ws://127.0.0.1:{port}", 4)
        async for frame in stream:
            got.append(frame)
            if len(got) == 3:
                break
        await stream.aclose()
        await asyncio.wait_for(closed.wait(), 3)  # the subscription ends with the viewer

    assert got == jpegs
    assert from_client[0] == {"type": "config", "maxFps": 4, "pacing": "ack"}
    # Each frame is acked when the viewer asks for the next one; the viewer left after the third.
    assert from_client[1:] == [{"type": "ack", "seq": 1}, {"type": "ack", "seq": 2}]
    assert not any(str(m.get("type", "")).startswith("input_") for m in from_client)


def test_parse_cli_output_and_boundaries():
    ok = parse_cli_output(
        json.dumps(
            {
                "success": True,
                "data": {"snapshot": '- link "Home" [ref=e1]', "origin": "https://example.com"},
                "_boundary": {"nonce": "abc123", "origin": "https://example.com"},
            }
        ).encode()
    )
    assert ok.ok and ok.error is None
    wrapped = wrap_boundaries(ok.text("snapshot"), ok.boundary, origin="https://example.com")
    assert wrapped.splitlines() == [
        "--- AGENT_BROWSER_PAGE_CONTENT nonce=abc123 origin=https://example.com ---",
        '- link "Home" [ref=e1]',
        "--- END_AGENT_BROWSER_PAGE_CONTENT nonce=abc123 ---",
    ]
    # Already wrapped text is left alone; missing boundary metadata gets a fresh nonce.
    assert wrap_boundaries(wrapped, ok.boundary, origin="x") == wrapped
    fresh = wrap_boundaries("hello", None, origin="https://a.test")
    assert fresh.startswith("--- AGENT_BROWSER_PAGE_CONTENT nonce=")
    assert "hello" in fresh and "END_AGENT_BROWSER_PAGE_CONTENT" in fresh
    assert wrap_boundaries("", None, origin="x") == ""

    failed = parse_cli_output(json.dumps({"success": False, "error": "Element not found"}).encode())
    assert not failed.ok and failed.error == "Element not found"
    denied = parse_cli_output(
        json.dumps({"success": False, "error": {"message": "denied by policy"}}).encode()
    )
    assert denied.error == "denied by policy"
    garbage = parse_cli_output(b"panic!", b"daemon crashed", 101)
    assert not garbage.ok and "daemon crashed" in garbage.error

    url = parse_cli_output(json.dumps({"success": True, "data": {"url": "https://e.com/"}}).encode())
    assert url.text("url") == "https://e.com/"
    plain = parse_cli_output(json.dumps({"success": True, "data": "Title"}).encode())
    assert plain.text("title") == "Title"


def test_parse_tab_ids():
    assert parse_tab_ids({"tabs": [{"tabId": "t1", "label": "Home"}, {"tabId": "t3"}, {"tabId": "t2"}]}) == [
        "t1",
        "t2",
        "t3",
    ]
    assert parse_tab_ids([{"id": "t10"}, {"id": "t9"}]) == ["t9", "t10"]
    assert parse_tab_ids({"tabs": [{"tabId": "--evil"}, {"label": "t5"}]}) == []
    assert parse_tab_ids(None) == []


# --- AgentBrowserEngine orchestration, without Chrome -------------------------------------------


def _write(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


def _read(path: str) -> str:
    return Path(path).read_text()


class _Proc:
    returncode = None


class _Guard:
    alive = True

    async def close(self):
        self.alive = False


def _ab_engine(tmp_path, *, binary="agent-browser", action_timeout_s=5.0):
    engine = AgentBrowserEngine(binary=binary, work_root=str(tmp_path), action_timeout_s=action_timeout_s)
    session = _AbSession("s1", tmp_path, 9333, _Proc(), _Guard())
    engine._sessions["s1"] = session
    return engine, session


def _scripted_cli(engine, replies: dict, seen: list) -> None:
    async def cli(s, command, *, limit_s, extra_globals=()):
        seen.append((tuple(command), tuple(extra_globals)))
        if command[0] == "screenshot":
            _write(command[1], b"\xff\xd8shot")
        payload = replies.get(tuple(command), {"success": True, "data": {}})
        if payload == "timeout":
            return CliReply(ok=False, error=f"timed out after {limit_s:.0f}s")
        return parse_cli_output(json.dumps(payload).encode())

    engine._cli = cli


async def test_agent_browser_act_collects_page_state(tmp_path):
    engine, _ = _ab_engine(tmp_path)
    seen: list = []
    _scripted_cli(
        engine,
        {
            ("snapshot", "-i"): {
                "success": True,
                "data": {"snapshot": '- link "Docs" [ref=e2]'},
                "_boundary": {"nonce": "n1", "origin": "https://example.com"},
            },
            ("get", "url"): {"success": True, "data": {"url": "https://example.com/docs"}},
            ("get", "title"): {"success": True, "data": {"title": "Docs"}},
        },
        seen,
    )
    result = await engine.act("s1", "open", {"url": "https://example.com/docs"})
    assert result.url == "https://example.com/docs"
    assert result.title == "Docs"
    assert result.snapshot.startswith(
        "--- AGENT_BROWSER_PAGE_CONTENT nonce=n1 origin=https://example.com ---"
    )
    assert result.screenshot == b"\xff\xd8shot"
    assert result.error is None
    commands = [c for c, _ in seen]
    assert commands[0] == ("open", "https://example.com/docs")
    assert ("snapshot", "-i") in commands
    shot = next(g for c, g in seen if c[0] == "screenshot")
    assert shot == ("--screenshot-format", "jpeg", "--screenshot-quality", "70")
    assert not list(tmp_path.glob("shot-*.jpg"))  # screenshots are not left on disk


async def test_agent_browser_click_follows_a_new_tab(tmp_path):
    engine, _ = _ab_engine(tmp_path)
    seen: list = []
    tabs = {"success": True, "data": {"tabs": [{"tabId": "t1"}, {"tabId": "t2"}]}}
    _scripted_cli(engine, {("tab",): tabs}, seen)
    await engine.act("s1", "click", {"ref": "@e3"})
    commands = [c for c, _ in seen]
    assert commands[:4] == [("click", "@e3"), ("tab",), ("tab", "t2"), ("tab", "close", "t1")]


async def test_agent_browser_timeout_is_an_actionable_error(tmp_path):
    engine, _ = _ab_engine(tmp_path)
    _scripted_cli(engine, {("open", "https://slow.example/"): "timeout"}, [])
    result = await engine.act("s1", "open", {"url": "https://slow.example/"})
    assert result.error.startswith("open timed out after 3s")
    assert "back" in result.error


async def test_agent_browser_dead_guard_is_session_gone(tmp_path):
    engine, session = _ab_engine(tmp_path)
    session.guard.alive = False  # unguarded pages must not be driven
    with pytest.raises(SessionGone):
        await engine.act("s1", "snapshot", {})


async def test_agent_browser_cli_runs_and_times_out(tmp_path):
    script = tmp_path / "fake-agent-browser"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "if sys.argv[-1] == 'slow': time.sleep(10)\n"
        "print(json.dumps({'success': True, 'data': {'argv': sys.argv[1:]}}))\n"
    )
    script.chmod(0o755)
    engine, session = _ab_engine(tmp_path, binary=str(script))
    reply = await engine._cli(session, ["back"], limit_s=10)
    assert reply.ok
    argv = reply.data["argv"]
    assert argv[:5] == ["--cdp", "9333", "--session", "s1", "--json"]
    policy = argv[argv.index("--action-policy") + 1]
    assert json.loads(_read(policy)) == json.loads(action_policy_json())
    assert argv[-1] == "back"

    t0 = time.monotonic()
    reply = await engine._cli(session, ["slow"], limit_s=0.3)
    assert time.monotonic() - t0 < 5
    assert not reply.ok and reply.error.startswith("timed out")

    missing_engine, _ = _ab_engine(tmp_path, binary=str(tmp_path / "nope"))
    missing = await missing_engine._cli(session, ["back"], limit_s=1)
    assert not missing.ok and "could not run" in missing.error
