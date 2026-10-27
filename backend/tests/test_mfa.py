"""SEC-22: optional TOTP two-factor authentication."""

from __future__ import annotations

import base64

import pytest

from argus.api.v1 import mfa as mfa_api
from argus.core import totp
from tests.conftest import register_and_login

PW = "correct horse battery staple"


def test_rfc6238_vectors():
    secret = base64.b32encode(b"12345678901234567890").decode()
    # RFC 6238 appendix B (SHA-1), last six digits.
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    assert totp.code_at(secret, 2000000000 // 30) == "279037"


def test_match_window_and_format():
    secret = totp.generate_secret()
    now = 1_700_000_000.0
    step = totp.counter_at(now)
    assert totp.match(secret, totp.code_at(secret, step), now) == step
    assert totp.match(secret, totp.code_at(secret, step - 1), now) == step - 1
    assert totp.match(secret, totp.code_at(secret, step + 1), now) == step + 1
    assert totp.match(secret, totp.code_at(secret, step + 2), now) is None
    code = totp.code_at(secret, step)
    assert totp.match(secret, f"{code[:3]} {code[3:]}", now) == step
    assert totp.match(secret, "12345", now) is None
    uri = totp.provisioning_uri(secret, "a@example.com", "Argus Council")
    assert uri.startswith("otpauth://totp/Argus%20Council%3Aa%40example.com?") and f"secret={secret}" in uri


def test_recovery_codes_are_random_and_hashed():
    codes = totp.new_recovery_codes()
    assert len(set(codes)) == 10 and all(len(c) == 14 for c in codes)
    assert totp.hash_recovery_code(codes[0]) == totp.hash_recovery_code(codes[0].upper())


@pytest.fixture
def no_limit(monkeypatch):
    async def none(*a, **k):
        return None

    monkeypatch.setattr(mfa_api, "_limit", none)


async def _enrol(client, h) -> tuple[str, list[str]]:
    r = await client.post("/api/v1/auth/mfa/setup", headers=h, json={"password": PW})
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    r = await client.post("/api/v1/auth/mfa/enable", headers=h, json={"code": _code(secret)})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


def _code(secret: str, offset: int = 0) -> str:
    return totp.code_at(secret, totp.counter_at() + offset)


async def _password_step(client, email="admin@example.com") -> dict:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()


async def test_enrolment_and_two_step_sign_in(client, auth, no_limit):
    r = await client.post("/api/v1/auth/mfa/setup", headers=auth, json={"password": "wrong password!"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_password"
    r = await client.post("/api/v1/auth/mfa/setup", headers=auth, json={"password": PW})
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    status = (await client.get("/api/v1/auth/mfa", headers=auth)).json()
    assert status == {"enabled": False, "pending": True, "recovery_codes_left": 0}
    # Still off until a code proves the app has the secret: sign-in is unchanged.
    assert "access_token" in await _password_step(client)
    r = await client.post("/api/v1/auth/mfa/enable", headers=auth, json={"code": "000000"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_mfa_code"
    r = await client.post("/api/v1/auth/mfa/enable", headers=auth, json={"code": _code(secret)})
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10
    me = (await client.get("/api/v1/auth/me", headers=auth)).json()
    assert me["mfa_enabled"] is True

    # The password alone no longer yields tokens.
    step = await _password_step(client)
    assert step["mfa_required"] is True and "access_token" not in step
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {step['mfa_token']}"})
    ).status_code == 401
    r = await client.post("/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": "000000"})
    assert r.status_code == 400
    r = await client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": _code(secret, 1)}
    )
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    ).status_code == 200
    # A completed challenge cannot mint a second session.
    r = await client.post("/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": codes[0]})
    assert r.status_code == 401

    # An authenticator code works once.
    step = await _password_step(client)
    r = await client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": _code(secret, 1)}
    )
    assert r.status_code == 400 and "already used" in r.json()["error"]["message"]
    # A recovery code works once.
    r = await client.post("/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": codes[0]})
    assert r.status_code == 200
    step = await _password_step(client)
    r = await client.post("/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": codes[0]})
    assert r.status_code == 400
    assert (await client.get("/api/v1/auth/mfa", headers=auth)).json()["recovery_codes_left"] == 9

    # Turning it off needs the password and a second factor.
    r = await client.post("/api/v1/auth/mfa/disable", headers=auth, json={"password": PW, "code": "000000"})
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/mfa/disable", headers=auth, json={"password": PW, "code": codes[1]})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert "access_token" in await _password_step(client)


async def test_challenge_allows_limited_attempts(client, auth, no_limit):
    secret, _ = await _enrol(client, auth)
    step = await _password_step(client)
    for _ in range(mfa_api.MAX_ATTEMPTS_PER_CHALLENGE):
        r = await client.post(
            "/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": "000000"}
        )
        assert r.status_code == 400
    r = await client.post(
        "/api/v1/auth/mfa/verify", json={"mfa_token": step["mfa_token"], "code": _code(secret, -1)}
    )
    assert r.status_code == 401


async def test_verify_is_rate_limited_per_account(client, auth, monkeypatch):
    import argus.cache.store as store

    monkeypatch.setattr(store.time, "time", lambda: 1_700_000_000.0)
    r = await client.post("/api/v1/auth/mfa/setup", headers=auth, json={"password": PW})
    assert r.status_code == 200
    codes = [
        (await client.post("/api/v1/auth/mfa/enable", headers=auth, json={"code": "000000"})).status_code
        for _ in range(6)
    ]
    assert 429 in codes


async def test_enabling_mfa_signs_out_other_sessions(client, auth, no_limit, monkeypatch):
    import time
    from types import SimpleNamespace

    import argus.services.sessions as sessions

    r = await client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PW})
    other = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # Tokens carry whole-second iat: put the cutoff strictly after both logins.
    later = time.time() + 5
    monkeypatch.setattr(sessions, "time", SimpleNamespace(time=lambda: later))
    await _enrol(client, auth)
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=other)).status_code == 401


async def test_mfa_management_needs_a_session(client, auth):
    r = await client.post("/api/v1/auth/api-keys", headers=auth, json={"name": "k", "scopes": ["*"]})
    key = {"Authorization": f"Bearer {r.json()['key']}"}
    assert (
        await client.post("/api/v1/auth/mfa/setup", headers=key, json={"password": PW})
    ).status_code == 403
    assert (await client.get("/api/v1/auth/mfa", headers=key)).status_code == 403


async def test_admin_can_reset_a_users_mfa(client, auth, no_limit):
    user = await register_and_login(client, "lost@example.com")
    await _enrol(client, user)
    uid = (await client.get("/api/v1/auth/me", headers=user)).json()["id"]
    users = {u["email"]: u for u in (await client.get("/api/v1/admin/users", headers=auth)).json()}
    assert users["lost@example.com"]["mfa_enabled"] is True
    r = await client.patch(f"/api/v1/admin/users/{uid}", headers=auth, json={"reset_mfa": True})
    assert r.status_code == 200 and r.json()["mfa_enabled"] is False
    assert "access_token" in await _password_step(client, "lost@example.com")


def test_export_never_includes_mfa_material():
    from argus.services.account_export import _secret

    assert _secret("totp_secret_enc") and _secret("totp_recovery") and not _secret("totp_enabled")
