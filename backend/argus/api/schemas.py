"""Request/response models for the public HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from argus.core.security import WRITE_SCOPES

CouncilMode = Literal["general", "research", "coding", "chat"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int


class UserOut(ORM):
    id: uuid.UUID
    email: str
    display_name: str
    is_admin: bool
    created_at: datetime


class ProfilePatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    current_password: str | None = Field(default=None, max_length=256)
    new_password: str | None = Field(default=None, min_length=12, max_length=256)

    @model_validator(mode="after")
    def _password_pair(self) -> ProfilePatch:
        if self.new_password is not None and not self.current_password:
            raise ValueError("current_password is required to set a new password")
        return self


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["*"], max_length=20)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, v: list[str]) -> list[str]:
        # "read" is accepted as an explicit marker for read-only keys; it grants nothing extra.
        allowed = {"*", "read", *WRITE_SCOPES}
        unknown = sorted(set(v) - allowed)
        if unknown:
            raise ValueError(f"Unknown scopes: {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")
        return sorted(set(v))


class ApiKeyOut(ORM):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked: bool


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Plaintext key. Shown exactly once.")


# ---------------------------------------------------------------- councils
_NAME = Field(min_length=1, max_length=80, pattern=r"^[\w .\-]+$")


class MemberIn(BaseModel):
    name: str = _NAME
    role: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    provider: str = Field(default="mock", max_length=40)
    model: str = Field(default="mock-1", max_length=120)
    system_prompt: str = Field(default="", max_length=20_000)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class MemberPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[\w .\-]+$")
    role: str | None = Field(default=None, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    provider: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=120)
    system_prompt: str | None = Field(default=None, max_length=20_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    enabled: bool | None = None
    position: int | None = Field(default=None, ge=0)
    config: dict[str, Any] | None = None


class MemberOut(ORM):
    id: uuid.UUID
    name: str
    role: str
    provider: str
    model: str
    system_prompt: str
    temperature: float
    position: int
    enabled: bool
    config: dict[str, Any]


class CouncilIn(BaseModel):
    name: str = _NAME
    description: str = Field(default="", max_length=4000)
    mode: CouncilMode = "general"
    min_size: int = Field(default=1, ge=1, le=64)
    max_size: int = Field(default=12, ge=1, le=64)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    members: list[MemberIn] | None = Field(
        default=None, description="Explicit seats. Omit to seat the default roster for the mode."
    )
    size: int | None = Field(default=None, ge=1, le=64, description="Seats to fill from the default roster.")
    default_provider: str = "mock"
    default_model: str = "mock-1"

    @model_validator(mode="after")
    def _bounds(self) -> CouncilIn:
        if self.min_size > self.max_size:
            raise ValueError("min_size must be <= max_size")
        n = len(self.members) if self.members is not None else self.size
        if n is not None and not (self.min_size <= n <= self.max_size):
            raise ValueError(f"council size {n} outside [{self.min_size}, {self.max_size}]")
        return self


class CouncilPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[\w .\-]+$")
    description: str | None = Field(default=None, max_length=4000)
    mode: CouncilMode | None = None
    min_size: int | None = Field(default=None, ge=1, le=64)
    max_size: int | None = Field(default=None, ge=1, le=64)
    guardrails: dict[str, Any] | None = None


class ResizeIn(BaseModel):
    size: int = Field(ge=1, le=64)
    provider: str | None = None
    model: str | None = None


class CouncilOut(ORM):
    id: uuid.UUID
    name: str
    description: str
    mode: str
    min_size: int
    max_size: int
    guardrails: dict[str, Any]
    parent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    members: list[MemberOut]

    @field_validator("members", mode="before")
    @classmethod
    def _sorted(cls, v: Any) -> Any:
        return sorted(v, key=lambda m: m.position) if isinstance(v, list) else v


# ---------------------------------------------------------------- providers
class ProviderConfigIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    kind: str = Field(max_length=40)
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    default_model: str | None = Field(default=None, max_length=120)
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class ProviderConfigPatch(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    default_model: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    options: dict[str, Any] | None = None


class ProviderConfigOut(ORM):
    id: uuid.UUID
    name: str
    kind: str
    base_url: str | None
    default_model: str | None
    enabled: bool
    options: dict[str, Any]
    has_api_key: bool = False
    created_at: datetime
    updated_at: datetime


class ChatMessageIn(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=200_000)


class CompleteIn(BaseModel):
    provider: str = Field(default="mock", max_length=64)
    model: str = Field(default="mock-1", max_length=120)
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=200)
    max_tokens: int = Field(default=4096, ge=1, le=128_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    cache: bool | None = None
    fallbacks: list[dict[str, str]] = Field(default_factory=list, max_length=5)


# ---------------------------------------------------------------- runs
OutputFormat = Literal["txt", "md", "latex", "pdf", "html", "docx", "json"]


class RunIn(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=20_000)
    mode: CouncilMode = "general"
    council_id: uuid.UUID | None = None
    size: int | None = Field(default=None, ge=1, le=64)
    provider: str = Field(default="mock", max_length=64)
    model: str = Field(default="mock-1", max_length=120)
    outputs: list[OutputFormat] = Field(default_factory=lambda: ["md"], max_length=7)
    guardrails: dict[str, Any] = Field(default_factory=dict)


class RunOut(ORM):
    id: uuid.UUID
    council_id: uuid.UUID | None
    parent_run_id: uuid.UUID | None
    topic: str
    description: str
    mode: str
    status: str
    outputs: list[str]
    guardrails: dict[str, Any]
    usage: dict[str, Any]
    error: str | None
    last_seq: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunDetail(RunOut):
    result: str
    council_snapshot: dict[str, Any]


class ExportIn(BaseModel):
    format: OutputFormat


class InterventionIn(BaseModel):
    kind: Literal["message", "intercept", "hold", "release", "correct", "halt_all", "release_all"]
    target: dict[str, Any] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=20_000)
    scope: Literal["thought", "member", "council"] | None = None

    @model_validator(mode="after")
    def _needs_text(self) -> InterventionIn:
        if self.kind in ("message", "correct") and not self.text:
            raise ValueError(f"'{self.kind}' requires text")
        return self


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    edited_args: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------- skills
class SkillIn(BaseModel):
    skill_md: str = Field(min_length=1, max_length=200_000)
    files: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class SkillPatch(BaseModel):
    enabled: bool


# ---------------------------------------------------------------- connections
_CONN_NAME = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_\-]*$")


class McpServerIn(BaseModel):
    name: str = _CONN_NAME
    transport: Literal["http", "stdio"] = "http"
    url: str | None = Field(default=None, max_length=500)
    command: list[str] = Field(default_factory=list, max_length=50)
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra headers (e.g. Authorization); "
        "for stdio, environment variables. Stored encrypted, never returned.",
    )
    tool_allow: list[str] = Field(default_factory=lambda: ["*"], max_length=200)
    risk: list[str] = Field(default_factory=lambda: ["network"], max_length=20)
    enabled: bool = True


class McpServerPatch(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] | None = None
    tool_allow: list[str] | None = Field(default=None, max_length=200)
    risk: list[str] | None = Field(default=None, max_length=20)
    enabled: bool | None = None


class RemoteAgentIn(BaseModel):
    name: str = _CONN_NAME
    protocol: Literal["a2a", "acp"]
    url: str = Field(max_length=500, description="A2A JSON-RPC endpoint or ACP base URL")
    agent_name: str | None = Field(default=None, max_length=120, description="Required for ACP")
    token: str | None = Field(default=None, max_length=4000)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True

    @model_validator(mode="after")
    def _acp_name(self) -> RemoteAgentIn:
        if self.protocol == "acp" and not self.agent_name:
            raise ValueError("agent_name is required for ACP agents")
        return self


class RemoteAgentPatch(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=4000)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


# ---------------------------------------------------------------- webhooks
class WebhookIn(BaseModel):
    url: str = Field(max_length=500)
    events: list[str] = Field(min_length=1, max_length=20)
    description: str = Field(default="", max_length=200)
    enabled: bool = True


class WebhookPatch(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    events: list[str] | None = Field(default=None, min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
