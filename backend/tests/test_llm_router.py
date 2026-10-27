import pytest

from argus.cache.store import Cache, MemoryBackend
from argus.core.crypto import SecretBox, mask
from argus.core.errors import NotFound
from argus.providers.base import ProviderError
from argus.providers.mock import MockProvider
from argus.providers.types import CompletionRequest, Message
from argus.services.llm import (
    InvalidProviderURL,
    LLMRouter,
    Route,
    UsageLedger,
    system_providers_from_env,
    validate_base_url,
)


class Failing(MockProvider):
    def __init__(self, retryable):
        super().__init__("bad")
        self.retryable = retryable
        self.n = 0

    async def complete(self, request):
        self.n += 1
        raise ProviderError("boom", provider="bad", retryable=self.retryable)


def router(settings, **system):
    settings.llm_max_retries = 2
    return LLMRouter(settings, Cache(MemoryBackend()), system={"mock": MockProvider(), **system})


def creq(**kw):
    return CompletionRequest(model="mock-1", messages=[Message.user("hello")], **kw)


async def test_fallback_on_failure(settings, monkeypatch):
    import argus.providers.base as base

    async def nosleep(_):
        pass

    monkeypatch.setattr(base.asyncio, "sleep", nosleep)
    bad = Failing(retryable=True)
    r = router(settings, bad=bad)
    ledger = UsageLedger()
    resp = await r.complete(creq(), Route("bad", "x"), fallbacks=[Route("mock", "mock-1")], ledger=ledger)
    assert resp.provider == "mock" and bad.n == 2  # retried, then fell back
    assert ledger.calls == 1 and ledger.total.output_tokens > 0


async def test_all_routes_fail(settings):
    r = router(settings, bad=Failing(retryable=False))
    with pytest.raises(ProviderError):
        await r.complete(creq(), Route("bad", "x"))
    with pytest.raises(NotFound):
        await r.complete(creq(), Route("nope", "x"))


async def test_cache_only_when_deterministic(settings):
    mock = MockProvider()
    r = router(settings, mock=mock)
    a = await r.complete(creq(temperature=0), Route("mock", "mock-1"))
    b = await r.complete(creq(temperature=0), Route("mock", "mock-1"))
    assert not a.cached and b.cached and b.usage.cost_usd == 0 and len(mock.calls) == 1
    await r.complete(creq(temperature=0.5), Route("mock", "mock-1"))
    await r.complete(creq(temperature=0.5), Route("mock", "mock-1"))
    assert len(mock.calls) == 3
    await r.complete(creq(metadata={"cache": True}), Route("mock", "mock-1"))
    await r.complete(creq(metadata={"cache": True}), Route("mock", "mock-1"))
    assert len(mock.calls) == 4


def test_system_providers_from_env():
    found = system_providers_from_env(
        {
            "ANTHROPIC_API_KEY": "a",
            "ARGUS_OLLAMA_BASE_URL": "http://o:11434/v1",
            "GOOGLE_API_KEY": "g",
            "ARGUS_GROQ_API_KEY": "",
        }
    )
    assert set(found) == {"mock", "anthropic", "ollama", "gemini"}


def test_validate_base_url():
    assert validate_base_url("http://ollama:11434/v1/", True) == "http://ollama:11434/v1"
    for bad in ("ftp://x", "notaurl", "http://user:pw@host/v1"):
        with pytest.raises(InvalidProviderURL):
            validate_base_url(bad, True)
    with pytest.raises(InvalidProviderURL):
        validate_base_url("http://127.0.0.1:8000/v1", False)


def test_secret_box(settings):
    box = SecretBox(settings)
    token = box.encrypt("sk-secret")
    assert "sk-secret" not in token and box.decrypt(token) == "sk-secret"
    other = settings.model_copy(update={"secret_key": settings.secret_key.__class__("z" * 40)})
    with pytest.raises(ValueError):
        SecretBox(other).decrypt(token)
    assert mask("sk-abcdefghijklmnop") == "sk-a…mnop"


# ------------------------------------------------------------------ API
async def test_provider_crud_api(client, auth):
    r = await client.get("/api/v1/providers/kinds", headers=auth)
    kinds = {k["key"] for k in r.json()["kinds"]}
    assert {"anthropic", "openai", "gemini", "ollama", "vllm", "lmstudio", "mock"} <= kinds
    assert "mock" in r.json()["system_providers"]

    body = {"name": "my-openai", "kind": "openai", "api_key": "sk-supersecretvalue", "default_model": "gpt-x"}
    r = await client.post("/api/v1/providers", json=body, headers=auth)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["has_api_key"] is True and "api_key" not in out and "sk-super" not in r.text
    pid = out["id"]
    assert (await client.post("/api/v1/providers", json=body, headers=auth)).status_code == 409

    r = await client.patch(f"/api/v1/providers/{pid}", json={"api_key": None, "enabled": False}, headers=auth)
    assert r.json()["has_api_key"] is False and r.json()["enabled"] is False
    r = await client.post(
        "/api/v1/llm/complete",
        headers=auth,
        json={"provider": "my-openai", "model": "gpt-x", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 502  # disabled provider

    assert (await client.delete(f"/api/v1/providers/{pid}", headers=auth)).status_code == 204
    assert (await client.get("/api/v1/providers", headers=auth)).json() == []


async def test_provider_validation(client, auth):
    r = await client.post("/api/v1/providers", headers=auth, json={"name": "x", "kind": "nope"})
    assert r.status_code == 400
    r = await client.post("/api/v1/providers", headers=auth, json={"name": "x", "kind": "openai_compatible"})
    assert r.status_code == 400
    r = await client.post(
        "/api/v1/providers",
        headers=auth,
        json={"name": "x", "kind": "vllm", "base_url": "file:///etc/passwd"},
    )
    assert r.status_code == 400
    r = await client.post("/api/v1/providers", headers=auth, json={"name": "Bad Name!", "kind": "mock"})
    assert r.status_code == 422


async def test_complete_endpoint_mock(client, auth):
    r = await client.post(
        "/api/v1/llm/complete",
        headers=auth,
        json={"messages": [{"role": "user", "content": "ping"}], "temperature": 0},
    )
    assert r.status_code == 200
    assert "ping" in r.json()["content"] and r.json()["cached"] is False
    r = await client.post(
        "/api/v1/llm/complete",
        headers=auth,
        json={"messages": [{"role": "user", "content": "ping"}], "temperature": 0},
    )
    assert r.json()["cached"] is True


async def test_list_models_mock(client, auth):
    r = await client.get("/api/v1/providers/mock/models", headers=auth)
    assert r.json()["models"] == ["mock-1", "mock-large"]


async def test_user_provider_isolated(client, auth):
    from tests.conftest import register_and_login

    await client.post("/api/v1/providers", headers=auth, json={"name": "mine", "kind": "mock"})
    other = await register_and_login(client, "o@example.com")
    r = await client.get("/api/v1/providers/mine/models", headers=other)
    assert r.status_code == 404
