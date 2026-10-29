"""With the mock provider disabled (every non-test install) it is not offered anywhere."""

import pytest

from argus.core.config import Settings
from argus.services.llm import system_providers_from_env


@pytest.fixture
def settings(settings: Settings) -> Settings:
    settings.enable_mock_provider = False
    return settings


def test_default_is_off_outside_tests():
    assert Settings(env="development").mock_provider_enabled is False
    assert Settings(env="test").mock_provider_enabled is True
    assert Settings(env="development", enable_mock_provider=True).mock_provider_enabled is True
    assert "mock" not in system_providers_from_env({}, include_mock=False)


async def test_mock_hidden_from_kinds_and_unusable(client, auth):
    body = (await client.get("/api/v1/providers/kinds", headers=auth)).json()
    assert "mock" not in {k["key"] for k in body["kinds"]}
    assert "mock" not in body["system_providers"]
    # Hosted kinds now come with suggestions, so pickers and tests have a model to offer.
    assert all(k["suggested_models"] for k in body["kinds"] if k["key"] in {"openai", "gemini", "mistral"})

    r = await client.post("/api/v1/providers", json={"name": "demo", "kind": "mock"}, headers=auth)
    assert r.status_code == 400
    r = await client.post(
        "/api/v1/llm/complete",
        json={"provider": "mock", "model": "mock-1", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth,
    )
    assert r.status_code == 404
    assert "mock provider is disabled" in r.json()["error"]["message"]
