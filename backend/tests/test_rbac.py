from __future__ import annotations

import uuid

from sqlalchemy import select

from argus.db.models import AuditEvent, User
from tests.conftest import register_and_login

ADMIN = "/api/v1/admin/users"


async def _me(client, headers) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _users(client, headers) -> dict[str, dict]:
    r = await client.get(ADMIN, headers=headers)
    assert r.status_code == 200, r.text
    return {u["email"]: u for u in r.json()}


async def _make_viewer(client, admin_headers, email="viewer@example.com") -> tuple[dict, str]:
    h = await register_and_login(client, email)
    uid = (await _me(client, h))["id"]
    r = await client.patch(f"{ADMIN}/{uid}", json={"role": "viewer"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "viewer"
    return h, uid


async def test_roles_on_me(client, auth):
    me = await _me(client, auth)
    assert me["role"] == "admin" and me["is_admin"] is True
    h2 = await register_and_login(client, "member@example.com")
    me2 = await _me(client, h2)
    assert me2["role"] == "member" and me2["is_admin"] is False


async def test_is_admin_compat_property():
    u = User(email="x@example.com", password_hash="x", is_admin=True)
    assert u.role == "admin" and u.is_admin
    u.is_admin = False
    assert u.role == "member"
    u.role = "viewer"
    u.is_admin = False
    assert u.role == "viewer"


async def test_viewer_blocked_on_writes(client, auth):
    vh, _ = await _make_viewer(client, auth)
    # Reads work.
    assert (await client.get("/api/v1/councils", headers=vh)).status_code == 200
    assert (await client.get("/api/v1/runs", headers=vh)).status_code == 200
    # Writes are refused with a clear message.
    r = await client.post("/api/v1/councils", json={"name": "nope"}, headers=vh)
    assert r.status_code == 403
    assert "Viewers have read-only access" in r.text
    r = await client.post("/api/v1/runs", json={"topic": "x"}, headers=vh)
    assert r.status_code == 403
    r = await client.post(
        f"/api/v1/runs/{uuid.uuid4()}/interventions", json={"kind": "message", "text": "hi"}, headers=vh
    )
    assert r.status_code == 403
    assert "Viewers have read-only access" in r.text
    # Admin API is off-limits.
    assert (await client.get(ADMIN, headers=vh)).status_code == 403


async def test_viewer_api_keys_read_only(client, auth):
    vh, _ = await _make_viewer(client, auth)
    r = await client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=vh)  # default ["*"]
    assert r.status_code == 403
    r = await client.post("/api/v1/auth/api-keys", json={"name": "k", "scopes": ["runs:write"]}, headers=vh)
    assert r.status_code == 403
    r = await client.post("/api/v1/auth/api-keys", json={"name": "k", "scopes": ["read"]}, headers=vh)
    assert r.status_code == 201, r.text
    kh = {"Authorization": f"Bearer {r.json()['key']}"}
    assert (await client.get("/api/v1/councils", headers=kh)).status_code == 200
    assert (await client.post("/api/v1/councils", json={"name": "nope"}, headers=kh)).status_code == 403


async def test_demoted_user_full_scope_key_becomes_read_only(client, auth):
    mh = await register_and_login(client, "member@example.com")
    r = await client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=mh)
    assert r.status_code == 201
    kh = {"Authorization": f"Bearer {r.json()['key']}"}
    assert (await client.post("/api/v1/councils", json={"name": "ok"}, headers=kh)).status_code == 201
    uid = (await _me(client, mh))["id"]
    assert (await client.patch(f"{ADMIN}/{uid}", json={"role": "viewer"}, headers=auth)).status_code == 200
    r = await client.post("/api/v1/councils", json={"name": "later"}, headers=kh)
    assert r.status_code == 403


async def test_admin_user_management(client, auth, app):
    mh = await register_and_login(client, "member@example.com")
    users = await _users(client, auth)
    assert set(users) == {"admin@example.com", "member@example.com"}
    admin_row, member_row = users["admin@example.com"], users["member@example.com"]
    assert admin_row["role"] == "admin" and member_row["role"] == "member"
    assert member_row["run_count"] == 0 and member_row["is_active"] is True
    assert admin_row["last_login_at"] is not None
    assert {"id", "email", "display_name", "role", "is_admin", "is_active", "created_at", "run_count"} <= set(
        member_row
    )

    # Non-admins cannot list or patch.
    assert (await client.get(ADMIN, headers=mh)).status_code == 403
    r = await client.patch(f"{ADMIN}/{admin_row['id']}", json={"role": "viewer"}, headers=mh)
    assert r.status_code == 403

    # Promote to admin: is_admin follows.
    r = await client.patch(f"{ADMIN}/{member_row['id']}", json={"role": "admin"}, headers=auth)
    assert r.status_code == 200 and r.json()["is_admin"] is True
    assert (await client.get(ADMIN, headers=mh)).status_code == 200

    # Deactivate: the user can no longer authenticate.
    r = await client.patch(f"{ADMIN}/{member_row['id']}", json={"is_active": False}, headers=auth)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me", headers=mh)).status_code == 401

    # Validation.
    r = await client.patch(f"{ADMIN}/{member_row['id']}", json={"role": "owner"}, headers=auth)
    assert r.status_code == 422
    r = await client.patch(f"{ADMIN}/{uuid.uuid4()}", json={"role": "member"}, headers=auth)
    assert r.status_code == 404

    # Audit middleware recorded the role change.
    import asyncio

    while pending := [t for t in app.state.background if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)
    async with app.state.db.sessionmaker() as s:
        rows = (
            (await s.execute(select(AuditEvent).where(AuditEvent.action == "PATCH /admin/users/{user_id}")))
            .scalars()
            .all()
        )
    promoted = [
        e
        for e in rows
        if e.success and e.target_id == member_row["id"] and "role" in e.detail.get("changes", {})
    ]
    assert promoted and promoted[0].detail["changes"]["role"] == {"from": "member", "to": "admin"}


async def test_last_admin_guard(client, auth):
    me = await _me(client, auth)
    # Cannot demote or deactivate yourself.
    r = await client.patch(f"{ADMIN}/{me['id']}", json={"role": "member"}, headers=auth)
    assert r.status_code == 409
    r = await client.patch(f"{ADMIN}/{me['id']}", json={"is_active": False}, headers=auth)
    assert r.status_code == 409
    # A no-op patch on yourself is fine.
    r = await client.patch(f"{ADMIN}/{me['id']}", json={"role": "admin", "is_active": True}, headers=auth)
    assert r.status_code == 200

    # With a second admin, the first may be demoted by the second...
    h2 = await register_and_login(client, "second@example.com")
    uid2 = (await _me(client, h2))["id"]
    assert (await client.patch(f"{ADMIN}/{uid2}", json={"role": "admin"}, headers=auth)).status_code == 200
    r = await client.patch(f"{ADMIN}/{me['id']}", json={"role": "member"}, headers=h2)
    assert r.status_code == 200 and r.json()["role"] == "member"
    # ...but now the second is the last active admin and cannot be removed (also not by itself).
    r = await client.patch(f"{ADMIN}/{uid2}", json={"role": "viewer"}, headers=h2)
    assert r.status_code == 409
