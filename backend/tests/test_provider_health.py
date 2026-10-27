import uuid

import anthropic
import httpx
import httpx2
import pytest
from sqlalchemy import select

import argus.services.llm as llm_mod
from argus.db.models import ProviderConfig
from argus.providers.anthropic import DEFAULT_BASE_URL, AnthropicProvider
from argus.providers.base import ProviderError
from argus.providers.health import HealthResult, classify_error, failure, key_hint
from argus.providers.mock import MockProvider
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import Route


# ------------------------------------------------------------------ key hint
@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-…wxyz"),
        ("sk-123456789012", "sk-…9012"),  # 15 chars
        ("abcdefghijkl", "abc…ijkl"),  # exactly 12
        ("short-key", "…ey"),  # < 12 chars: only the last 2
        ("ab", "…ab"),
        ("", None),
        (None, None),
    ],
)
def test_key_hint(key, hint):
    assert key_hint(key) == hint


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ProviderError.from_status("p", 401, "invalid x-api-key"), "auth_failed"),
        (ProviderError.from_status("p", 403, "forbidden"), "auth_failed"),
        (ProviderError.from_status("p", 429, "slow down"), "rate_limited"),
        (ProviderError("p: connection error", provider="p", retryable=True), "unreachable"),
        (ProviderError("p: timeout", provider="p", retryable=True), "unreachable"),
        (ProviderError.from_status("p", 500, "oops"), "error"),
        (ProviderError.from_status("p", 404, "no such model"), "error"),
        (ProviderError("p: weird", provider="p"), "error"),
        (httpx.ConnectError("refused"), "unreachable"),
        (TimeoutError(), "unreachable"),
        (ValueError("bad"), "error"),
    ],
)
def test_classify_error(exc, status):
    got, detail = classify_error(exc)
    assert got == status
    assert len(detail) <= 300


def test_failure_detail_is_truncated():
    res = failure(ProviderError("x" * 1000, provider="p"), latency_ms=5)
    assert res.status == "error" and len(res.detail) == 300 and res.latency_ms == 5 and not res.ok


# ------------------------------------------------------------------ fake providers via the router
class Fake(MockProvider):
    """Mock that fails according to ``options.fail``: an HTTP status, "connect", or nothing."""

    def __init__(self, name, fail=None):
        super().__init__(name)
        self.fail = fail

    def _maybe_fail(self):
        if self.fail == "connect":
            raise ProviderError(f"{self.name}: connection error", provider=self.name, retryable=True)
        if self.fail:
            raise ProviderError.from_status(self.name, int(self.fail), "nope")

    async def complete(self, request):
        self._maybe_fail()
        return await super().complete(request)

    async def list_models(self):
        self._maybe_fail()
        return ["fake-1"]


@pytest.fixture
def fake_build(monkeypatch):
    def build(kind, *, name=None, api_key=None, base_url=None, options=None, guard_private=None):
        return Fake(name or kind, fail=(options or {}).get("fail"))

    monkeypatch.setattr(llm_mod, "build_provider", build)


async def _create(client, auth, name, **extra):
    body = {"name": name, "kind": "mock", "api_key": "sk-test-0123456789abcd", **extra}
    r = await client.post("/api/v1/providers", headers=auth, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ API
async def test_key_hint_in_responses(client, auth):
    out = await _create(client, auth, "p1")
    assert out["key_hint"] == "sk-…abcd" and out["has_api_key"] is True
    assert "0123456789" not in str(out)
    assert out["health_status"] == "unknown" and out["health_checked_at"] is None

    r = await client.patch(f"/api/v1/providers/{out['id']}", headers=auth, json={"api_key": "new-key"})
    assert r.json()["key_hint"] == "…ey"
    r = await client.patch(f"/api/v1/providers/{out['id']}", headers=auth, json={"api_key": None})
    assert r.json()["key_hint"] is None and r.json()["has_api_key"] is False
    listed = (await client.get("/api/v1/providers", headers=auth)).json()
    assert listed[0]["key_hint"] is None


async def test_key_hint_backfilled_for_legacy_rows(app, client, auth):
    out = await _create(client, auth, "legacy")
    async with app.state.db.sessionmaker() as s:
        cfg = await s.get(ProviderConfig, uuid.UUID(out["id"]))
        cfg.key_hint = None
        await s.commit()
    listed = (await client.get("/api/v1/providers", headers=auth)).json()
    assert listed[0]["key_hint"] == "sk-…abcd"


@pytest.mark.parametrize(
    ("fail", "status"),
    [
        (None, "ok"),
        ("401", "auth_failed"),
        ("403", "auth_failed"),
        ("429", "rate_limited"),
        ("connect", "unreachable"),
        ("500", "error"),
    ],
)
async def test_test_endpoint_classifies_and_persists(client, auth, fake_build, fail, status):
    out = await _create(client, auth, "p", options={"fail": fail} if fail else {})
    r = await client.post("/api/v1/providers/p/test", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == status and body["ok"] is (status == "ok")
    assert body["probe"] == "list_models" and body["persisted"] is True
    assert isinstance(body["latency_ms"], int) and body["checked_at"]
    if status == "ok":
        assert body["model_count"] == 1
    else:
        assert body["detail"]
    cfg = body["config"]
    assert cfg["health_status"] == status and cfg["health_checked_at"]
    assert (
        cfg["updated_at"].rstrip("Z")[:26] == out["updated_at"].rstrip("Z")[:26]
    )  # health writes don't count as edits
    listed = (await client.get("/api/v1/providers", headers=auth)).json()
    assert listed[0]["health_status"] == status


async def test_test_endpoint_completion_probe(client, auth, fake_build):
    await _create(client, auth, "p")
    r = await client.post("/api/v1/providers/p/test", headers=auth, json={"model": "fake-1"})
    body = r.json()
    assert body["probe"] == "completion" and body["model"] == "fake-1" and body["ok"] is True


async def test_test_endpoint_disabled_and_system_and_missing(client, auth, fake_build):
    await _create(client, auth, "off", enabled=False)
    r = await client.post("/api/v1/providers/off/test", headers=auth)
    assert r.json()["status"] == "ok"  # tests work before enabling
    r = await client.post("/api/v1/providers/mock/test", headers=auth)
    assert r.status_code == 200 and r.json()["persisted"] is False and r.json()["config"] is None
    assert r.json()["model_count"] == 2
    r = await client.post("/api/v1/providers/nope/test", headers=auth)
    assert r.status_code == 404


async def test_passive_health_from_real_calls(app, client, auth, fake_build):
    llm = app.state.llm
    await _create(client, auth, "bad", options={"fail": "401"})
    await _create(client, auth, "good")
    msgs = [{"role": "user", "content": "hi"}]
    r = await client.post(
        "/api/v1/llm/complete", headers=auth, json={"provider": "bad", "model": "m", "messages": msgs}
    )
    assert r.status_code == 502
    r = await client.post(
        "/api/v1/llm/complete", headers=auth, json={"provider": "good", "model": "m", "messages": msgs}
    )
    assert r.status_code == 200
    await llm.flush_health()
    assert llm.health_errors == []
    listed = {p["name"]: p for p in (await client.get("/api/v1/providers", headers=auth)).json()}
    assert listed["bad"]["health_status"] == "auth_failed"
    assert "401" in listed["bad"]["health_detail"]
    assert listed["good"]["health_status"] == "ok" and listed["good"]["health_latency_ms"] is not None


async def test_passive_health_throttled_and_nonblocking(app, auth, client, fake_build):
    llm = app.state.llm
    await _create(client, auth, "good")
    async with app.state.db.sessionmaker() as s:
        owner = (await s.execute(select(ProviderConfig.owner_id))).scalar_one()
    req = CompletionRequest(model="m", messages=[Message.user("x")])
    writes = []
    orig = llm._write_health

    async def counting(config_id, result):
        writes.append(result.status)
        await orig(config_id, result)

    llm._write_health = counting
    for _ in range(3):
        await llm.complete(req, Route("good", "m"), owner_id=owner)
    await llm.flush_health()
    assert writes == ["ok"]  # unchanged status is not rewritten within the interval


async def test_passive_health_write_errors_are_recorded(app, auth, client, fake_build):
    llm = app.state.llm

    async def boom(config_id, result):
        raise RuntimeError("db down")

    llm._write_health = boom
    provider = Fake("x")
    provider.config_id = uuid.uuid4()
    llm.note_health(provider, HealthResult("ok"))
    await llm.flush_health()
    assert len(llm.health_errors) == 1 and "db down" in str(llm.health_errors[0])
    llm.health_errors.clear()


# ------------------------------------------------------------------ anthropic: one client, one auth
def test_anthropic_ignores_ambient_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-jwt")
    p = AnthropicProvider(api_key="sk-ant-user-key-1234")
    assert str(p.client.base_url).rstrip("/") == DEFAULT_BASE_URL
    assert p.client.api_key == "sk-ant-user-key-1234" and p.client.auth_token is None
    keyless = AnthropicProvider()
    assert keyless.client.auth_token is None and keyless.client.credentials is None


async def test_anthropic_models_and_messages_share_auth():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.headers.get("authorization"), req.headers.get("x-api-key")))
        if req.url.path == "/v1/models":
            after = req.url.params.get("after_id")
            ids = ["m3"] if after else ["m1", "m2"]
            return httpx2.Response(
                200,
                json={
                    "data": [
                        {"id": i, "type": "model", "display_name": i, "created_at": "2026-01-01T00:00:00Z"}
                        for i in ids
                    ],
                    "has_more": not after,
                    "first_id": ids[0],
                    "last_id": ids[-1],
                },
            )
        return httpx2.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
        )

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-k",
        base_url=DEFAULT_BASE_URL,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    p = AnthropicProvider(client=client)
    assert await p.list_models() == ["m1", "m2", "m3"]
    with pytest.raises(ProviderError) as ei:
        await p.complete(CompletionRequest(model="claude-x", messages=[Message.user("hi")], max_tokens=5))
    assert classify_error(ei.value)[0] == "auth_failed"
    auths = {(a, k) for _, a, k in seen}
    assert len(auths) == 1  # identical credentials on every endpoint
