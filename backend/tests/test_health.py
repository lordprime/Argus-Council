async def test_live(client):
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Request-ID" in r.headers


async def test_ready(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": True, "cache": True}


async def test_request_id_sanitised(client):
    r = await client.get("/health/live", headers={"X-Request-ID": "bad id\r\ninjected"})
    assert r.headers["X-Request-ID"] != "bad id\r\ninjected"
    r = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


async def test_payload_too_large(client, settings):
    r = await client.post(
        "/api/v1/auth/login",
        content=b"x" * (settings.max_request_bytes + 1),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
