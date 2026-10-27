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


async def test_login_throttled(client, auth, monkeypatch):
    import argus.cache.store as store

    # Pin the clock: the limiter uses fixed one-minute windows, so real time could split attempts
    # across two windows and never trip the limit.
    monkeypatch.setattr(store.time, "time", lambda: 1_700_000_000.0)
    codes = [
        (
            await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "bad"})
        ).status_code
        for _ in range(12)
    ]
    assert 429 in codes


async def test_update_profile_and_password(client, auth):
    r = await client.patch("/api/v1/auth/me", headers=auth, json={"display_name": "  Ada  "})
    assert r.status_code == 200 and r.json()["display_name"] == "Ada"
    r = await client.patch("/api/v1/auth/me", headers=auth, json={"new_password": "brand new password!"})
    assert r.status_code == 422  # current password required
    r = await client.patch(
        "/api/v1/auth/me",
        headers=auth,
        json={"current_password": "wrong", "new_password": "brand new password!"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_password"
    r = await client.patch(
        "/api/v1/auth/me",
        headers=auth,
        json={"current_password": "correct horse battery staple", "new_password": "short"},
    )
    assert r.status_code == 422
    r = await client.patch(
        "/api/v1/auth/me",
        headers=auth,
        json={"current_password": "correct horse battery staple", "new_password": "brand new password!"},
    )
    assert r.status_code == 200
    old = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery staple"}
    )
    assert old.status_code == 401
    new = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "brand new password!"}
    )
    assert new.status_code == 200


async def test_api_key_cannot_change_password(client, auth):
    key = (await client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=auth)).json()["key"]
    r = await client.patch(
        "/api/v1/auth/me",
        headers={"X-API-Key": key},
        json={"current_password": "correct horse battery staple", "new_password": "brand new password!"},
    )
    assert r.status_code == 403


async def test_empty_scopes_are_read_only(client, auth):
    key = (
        await client.post("/api/v1/auth/api-keys", json={"name": "ro", "scopes": []}, headers=auth)
    ).json()["key"]
    kh = {"X-API-Key": key}
    assert (await client.get("/api/v1/runs", headers=kh)).status_code == 200
    r = await client.post("/api/v1/runs", headers=kh, json={"topic": "x", "mode": "chat", "size": 2})
    assert r.status_code == 403
    r = await client.post("/api/v1/providers", headers=kh, json={"name": "p", "kind": "mock"})
    assert r.status_code == 403


async def test_write_scopes_cover_every_router(client, auth):
    async def key(scopes):
        r = await client.post("/api/v1/auth/api-keys", json={"name": "k", "scopes": scopes}, headers=auth)
        assert r.status_code == 201, r.text
        return {"X-API-Key": r.json()["key"]}

    runs_only = await key(["runs:write"])
    council = {"name": "c", "members": [{"name": "a", "role": "worker", "provider": "mock", "model": "mock"}]}
    assert (await client.post("/api/v1/councils", json=council, headers=runs_only)).status_code == 403
    hook = {"url": "https://example.com/hook", "events": ["run.completed"]}
    assert (await client.post("/api/v1/webhooks", json=hook, headers=runs_only)).status_code == 403
    assert (await client.get("/api/v1/councils", headers=runs_only)).status_code == 200
    # Validation changes nothing, so it needs no write scope.
    r = await client.post("/api/v1/skills/validate", json={"skill_md": "x"}, headers=runs_only)
    assert r.status_code == 200
    councils = await key(["councils:write"])
    r = await client.post("/api/v1/councils", json=council, headers=councils)
    assert r.status_code == 201, r.text


async def test_api_keys_cannot_manage_the_account(client, auth):
    r = await client.post("/api/v1/auth/api-keys", json={"name": "k", "scopes": ["*"]}, headers=auth)
    full = {"X-API-Key": r.json()["key"]}
    key_id = r.json()["id"]
    assert (await client.post("/api/v1/auth/api-keys", json={"name": "x"}, headers=full)).status_code == 403
    assert (await client.delete(f"/api/v1/auth/api-keys/{key_id}", headers=full)).status_code == 403
    r = await client.patch(
        "/api/v1/auth/me", headers=full, json={"current_password": "x", "new_password": "brand new password!"}
    )
    assert r.status_code == 403


async def test_unknown_scope_rejected(client, auth):
    r = await client.post("/api/v1/auth/api-keys", json={"name": "k", "scopes": ["admin"]}, headers=auth)
    assert r.status_code == 422
