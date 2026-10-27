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
    # Multipart uploads (skill .zip import, knowledge documents); each handler also caps its own files.
    max_upload_bytes: int = 9 * 1024 * 1024

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

    encryption_key: SecretStr | None = None  # defaults to a key derived from secret_key

    # LLM layer
    llm_cache_ttl: int = 3600
    llm_max_retries: int = 3
    provider_allow_private_urls: bool = True  # self-hosted models usually live on private networks
    # Webhooks, HTTP MCP servers and remote A2A/ACP agents: private/loopback targets are refused
    # unless the operator opts in. Link-local (cloud metadata) addresses are refused regardless.
    connections_allow_private_urls: bool = False
    # web_fetch (driven by model output, so by whatever content the agents read): a run's guardrails
    # can only tighten this, never open private networks on their own.
    web_fetch_allow_private_urls: bool = False
    model_pricing: dict[str, dict[str, float]] = Field(default_factory=dict)

    # Research tools
    search_provider: Literal["searxng", "tavily", "brave"] | None = None
    search_api_key: SecretStr | None = None
    searxng_url: str | None = None

    # Code execution sandbox (separate container); local execution is for development only
    sandbox_url: str | None = None
    sandbox_token: SecretStr | None = None
    sandbox_local: bool = False

    # CLI agents: the vendors' official CLIs (claude, codex, gemini) run as council agents in the
    # separate agent runner service. Off unless the operator opts in. Each vendor's terms govern its
    # CLI; subscription logins are for the account owner's own use (multi-user: use API keys).
    cli_agents_enabled: bool = False
    cli_agents_allowed: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["claude", "codex", "gemini"]
    )
    # false: a CLI agent runs only with the user's own API key, never a subscription login.
    cli_agents_allow_subscription: bool = True
    cli_agents_timeout_s: int = Field(default=600, ge=30, le=3600)
    runner_url: str | None = None
    runner_token: SecretStr | None = None
    runner_local: bool = False  # development only: run the CLIs on this host; refused in production

    # Observability: when set, /metrics requires "Authorization: Bearer <token>"
    metrics_token: SecretStr | None = None

    # Storage
    data_dir: str = "./data"

    # Data retention (days; 0 = keep forever). See argus/services/retention.py.
    retention_run_days: int = Field(default=0, ge=0)  # finished runs + events + artifacts + blobs
    retention_event_days: int = Field(default=0, ge=0)  # event log of finished runs only
    retention_audit_days: int = Field(default=365, ge=0)
    # The hash-chained ledger is never pruned by default: verification replays it from genesis.
    retention_ledger_days: int = Field(default=0, ge=0)
    retention_batch_size: int = Field(default=500, ge=1, le=10_000)
    retention_interval_s: int = Field(default=3600, ge=60)
    retention_enabled: bool = True  # run the periodic retention task in this process
    export_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)  # cap for /me/export archives

    # Protocols
    mcp_allow_stdio: bool = False  # stdio MCP servers spawn local processes; admins only when enabled
    protocol_timeout_s: float = 60.0
    remote_agent_timeout_s: float = 600.0

    # Rate limiting (requests per minute per principal / IP)
    rate_limit_per_minute: int = 120

    @field_validator(
        "redis_url",
        "encryption_key",
        "bootstrap_admin_email",
        "bootstrap_admin_password",
        "search_provider",
        "search_api_key",
        "searxng_url",
        "sandbox_url",
        "sandbox_token",
        "runner_url",
        "runner_token",
        "metrics_token",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, v: object) -> object:
        # docker compose passes unset variables as empty strings
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("cli_agents_allowed", mode="before")
    @classmethod
    def _split_clis(cls, v: object) -> object:
        if isinstance(v, str):
            v = [c.strip().lower() for c in v.split(",") if c.strip()]
        if isinstance(v, list):
            unknown = sorted({str(c) for c in v} - {"claude", "codex", "gemini"})
            if unknown:
                raise ValueError(f"unknown CLI agents: {', '.join(unknown)}")
        return v

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
