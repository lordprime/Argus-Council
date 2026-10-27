"""M6 / L4: every error leaves the API as one JSON envelope carrying the request id."""

from __future__ import annotations

import logging

from argus.core.metrics import HTTP_REQUESTS


def _boom_route(app) -> None:
    async def boom() -> dict:
        raise RuntimeError("database exploded: secret-connection-string")

    app.add_api_route("/api/v1/_test/boom", boom, methods=["GET", "POST"])


async def test_unhandled_exception_returns_json_envelope_with_request_id(app, client):
    _boom_route(app)
    r = await client.get("/api/v1/_test/boom", headers={"X-Request-ID": "trace-500"})
    assert r.status_code == 500
    assert r.headers["X-Request-ID"] == "trace-500"
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {
        "error": {"code": "internal_error", "message": "Internal error", "request_id": "trace-500"}
    }
    # Internal detail never leaks to the client.
    assert "secret-connection-string" not in r.text
    # Security headers are still applied.
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_unhandled_exception_is_counted_in_http_metrics(app, client):
    _boom_route(app)
    before = HTTP_REQUESTS.labels("GET", "/api/v1/_test/boom", "500")._value.get()
    await client.get("/api/v1/_test/boom")
    after = HTTP_REQUESTS.labels("GET", "/api/v1/_test/boom", "500")._value.get()
    assert after == before + 1


async def test_unhandled_exception_logged_with_traceback_and_request_id(app, client, caplog):
    _boom_route(app)
    await client.get("/api/v1/_test/boom", headers={"X-Request-ID": "trace-log"})
    rec = next(
        r for r in caplog.records if isinstance(r.msg, dict) and r.msg["event"] == "http.unhandled_exception"
    )
    assert rec.msg["request_id"] == "trace-log"
    assert rec.msg["route"] == "/api/v1/_test/boom"
    assert rec.msg["exc_info"] is True and rec.levelname == "ERROR"


def test_exceptions_render_with_traceback_and_context(capsys):
    import json

    import structlog

    from argus.core.logging import configure_logging, get_logger

    configure_logging("INFO", json=True)
    structlog.contextvars.bind_contextvars(request_id="ctx-1")
    try:
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            get_logger("argus.test").exception("thing.failed")
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    parsed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert parsed["event"] == "thing.failed" and parsed["request_id"] == "ctx-1"
    assert "Traceback" in parsed["exception"] and "RuntimeError: kaboom" in parsed["exception"]


async def test_unhandled_exception_on_write_is_audited(app, client, auth):
    import asyncio

    _boom_route(app)
    r = await client.post("/api/v1/_test/boom", headers=auth)
    assert r.status_code == 500
    for _ in range(100):  # audit rows are written asynchronously
        events = (
            await client.get("/api/v1/audit", headers=auth, params={"all": "true", "success": "false"})
        ).json()["events"]
        row = next((e for e in events if e["action"] == "POST /api/v1/_test/boom"), None)
        if row:
            break
        await asyncio.sleep(0.05)
    assert row is not None and row["detail"]["status"] == 500


async def test_domain_error_envelope_includes_request_id(client):
    r = await client.get("/api/v1/councils", headers={"X-Request-ID": "trace-401"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["request_id"] == "trace-401"


async def test_validation_error_uses_envelope(client):
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "x"},
        headers={"X-Request-ID": "v1"},
    )
    assert r.status_code == 422
    body = r.json()
    assert "detail" not in body
    err = body["error"]
    assert err["code"] == "validation_error"
    assert err["request_id"] == "v1"
    assert "email" in err["message"]
    assert err["details"]["errors"] and {"loc", "msg", "type"} <= set(err["details"]["errors"][0])
    # Submitted values are never echoed back (passwords live in these bodies).
    assert "input" not in err["details"]["errors"][0]


async def test_http_exception_uses_envelope(client):
    r = await client.get("/api/v1/does-not-exist", headers={"X-Request-ID": "nf"})
    assert r.status_code == 404
    assert r.json()["error"] == {"code": "not_found", "message": "Not Found", "request_id": "nf"}
    r = await client.put("/health/live")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


async def test_payload_too_large_envelope_has_request_id(client, settings):
    r = await client.post(
        "/api/v1/auth/login",
        content=b"x" * (settings.max_request_bytes + 1),
        headers={"content-type": "application/json", "X-Request-ID": "big"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["request_id"] == "big"


def test_stdlib_logs_rendered_through_structlog(capsys):
    from argus.core.logging import configure_logging

    configure_logging("INFO", json=True)
    logging.getLogger("uvicorn.error").warning("plain stdlib line")
    out = capsys.readouterr().out.strip().splitlines()[-1]
    import json

    parsed = json.loads(out)
    assert parsed["event"] == "plain stdlib line"
    assert parsed["level"] == "warning"
    assert parsed["logger"] == "uvicorn.error"
