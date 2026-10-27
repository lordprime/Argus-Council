"""Request/response models for the public HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

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


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


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
