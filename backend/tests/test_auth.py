from tests.conftest import register_and_login


async def test_first_user_is_admin_and_me(client, auth):
    r = await client.get("/api/v1/auth/me", headers=auth)
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


async def test_second_user_not_admin(client, auth):
    h2 = await register_and_login(client, "user2@example.com")
    assert (await client.get("/api/v1/auth/me", headers=h2)).json()["is_admin"] is False


async def test_duplicate_email(client, auth):
    r = await client.post(
        "/api/v1/auth/register", json={"email": "ADMIN@example.com", "password": "another long password"}
    )
    assert r.status_code == 409


async def test_weak_password_rejected(client):
    r = await client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "short"})
    assert r.status_code == 422


async def test_bad_login(client, auth):
    r = await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "nope"})
    assert r.status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    assert r.status_code == 401


async def test_unauthenticated(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


async def test_refresh_rotation(client):
    pw = "correct horse battery staple"
    await client.post("/api/v1/auth/register", json={"email": "r@example.com", "password": pw})
    tokens = (await client.post("/api/v1/auth/login", json={"email": "r@example.com", "password": pw})).json()
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    # single use
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    # access token can't be used as refresh
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert r.status_code == 401


async def test_api_key_lifecycle(client, auth):
    r = await client.post("/api/v1/auth/api-keys", json={"name": "ci"}, headers=auth)
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("argus_")
    kh = {"X-API-Key": key}
    assert (await client.get("/api/v1/auth/me", headers=kh)).status_code == 200
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {key}"})
    ).status_code == 200
    # API keys cannot mint more keys
    assert (await client.post("/api/v1/auth/api-keys", json={"name": "x"}, headers=kh)).status_code == 403
    listed = (await client.get("/api/v1/auth/api-keys", headers=auth)).json()
    assert "key" not in listed[0] and listed[0]["prefix"] == key[:14]
    assert (await client.delete(f"/api/v1/auth/api-keys/{listed[0]['id']}", headers=auth)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=kh)).status_code == 401


async def test_registration_can_be_disabled(settings):
    from httpx import ASGITransport, AsyncClient

    from argus.main import create_app
    from tests.conftest import _reset_state

    settings.allow_registration = False
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await register_and_login(c)  # first user always allowed (bootstrap)
            r = await c.post("/api/v1/auth/register", json={"email": "b@example.com", "password": "x" * 12})
            assert r.status_code == 403
        await _reset_state(app)


async def test_login_throttled(client, auth):
    codes = [
        (
            await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "bad"})
        ).status_code
        for _ in range(12)
    ]
    assert 429 in codes
