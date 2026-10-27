import pytest

from argus.core.config import Settings
from argus.core.errors import Unauthorized
from argus.core.security import (
    create_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)


def test_password_roundtrip():
    h = hash_password("hunter2hunter2")
    assert verify_password("hunter2hunter2", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")


def test_token_type_enforced(settings):
    t = create_token(settings, "abc", "refresh")
    with pytest.raises(Unauthorized):
        decode_token(settings, t, "access")
    assert decode_token(settings, t, "refresh")["sub"] == "abc"


def test_token_wrong_secret(settings):
    t = create_token(settings, "abc", "access")
    other = settings.model_copy(update={"secret_key": settings.secret_key.__class__("y" * 40)})
    with pytest.raises(Unauthorized):
        decode_token(other, t, "access")


def test_api_key_hashing():
    plain, prefix, digest = generate_api_key()
    assert plain.startswith(prefix) and hash_api_key(plain) == digest and plain not in digest


def test_production_requires_secret():
    with pytest.raises(ValueError):
        Settings(env="production", database_url="sqlite+aiosqlite://")
    with pytest.raises(ValueError):
        Settings(env="production", secret_key="short")


def test_dev_gets_ephemeral_secret():
    a, b = Settings(env="development"), Settings(env="development")
    assert a.secret_key.get_secret_value() != b.secret_key.get_secret_value()


def test_cors_csv():
    s = Settings(cors_origins="https://a.com, https://b.com")
    assert s.cors_origins == ["https://a.com", "https://b.com"]


def test_cors_from_env(monkeypatch):
    monkeypatch.setenv("ARGUS_CORS_ORIGINS", "http://localhost:8080,https://x.io")
    assert Settings().cors_origins == ["http://localhost:8080", "https://x.io"]
    monkeypatch.setenv("ARGUS_CORS_ORIGINS", '["https://y.io"]')
    assert Settings().cors_origins == ["https://y.io"]


def test_empty_bootstrap_env_is_ignored(monkeypatch):
    monkeypatch.setenv("ARGUS_BOOTSTRAP_ADMIN_EMAIL", "")
    monkeypatch.setenv("ARGUS_BOOTSTRAP_ADMIN_PASSWORD", "")
    s = Settings()
    assert not (s.bootstrap_admin_email and s.bootstrap_admin_password)


def test_empty_optional_env_vars_are_unset(monkeypatch):
    for name in (
        "ARGUS_SEARCH_PROVIDER",
        "ARGUS_SEARCH_API_KEY",
        "ARGUS_SANDBOX_URL",
        "ARGUS_SANDBOX_TOKEN",
        "ARGUS_ENCRYPTION_KEY",
        "ARGUS_REDIS_URL",
        "ARGUS_SEARXNG_URL",
    ):
        monkeypatch.setenv(name, "")
    s = Settings()
    assert (
        s.search_provider is None
        and s.sandbox_url is None
        and s.encryption_key is None
        and s.redis_url is None
    )
