"""Runtime configuration, loaded from environment variables (prefix ``ARGUS_``)."""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_DEFAULT_SECRET = "change-me-in-production"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARGUS_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    app_name: str = "Argus Council"
    log_level: str = "INFO"
    log_json: bool = False

    # HTTP
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:8080"]
    )
    max_request_bytes: int = 2 * 1024 * 1024

    # Persistence
    database_url: str = "sqlite+aiosqlite:///./argus.db"
    redis_url: str | None = None
    cache_default_ttl: int = 300

    # Auth
    secret_key: SecretStr = SecretStr(_INSECURE_DEFAULT_SECRET)
    access_token_ttl_minutes: int = 60
    refresh_token_ttl_days: int = 14
    allow_registration: bool = True
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # Rate limiting (requests per minute per principal / IP)
    rate_limit_per_minute: int = 120

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> Settings:
        if self.secret_key.get_secret_value() == _INSECURE_DEFAULT_SECRET:
            if self.env == "production":
                raise ValueError("ARGUS_SECRET_KEY must be set to a strong random value in production")
            # Ephemeral per-process secret keeps dev usable without shipping a known key.
            self.secret_key = SecretStr(secrets.token_urlsafe(48))
        elif len(self.secret_key.get_secret_value()) < 32:
            raise ValueError("ARGUS_SECRET_KEY must be at least 32 characters")
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
