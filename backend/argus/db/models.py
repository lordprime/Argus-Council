"""ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from argus.db.base import Base, IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ApiKey(IdMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(32), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[Any]] = mapped_column(default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="api_keys")


class Council(IdMixin, TimestampMixin, Base):
    """A named, resizable group of agents that collaborates on runs."""

    __tablename__ = "councils"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(32), default="general")
    min_size: Mapped[int] = mapped_column(Integer, default=1)
    max_size: Mapped[int] = mapped_column(Integer, default=12)
    guardrails: Mapped[dict[str, Any]] = mapped_column(default=dict)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("councils.id", ondelete="SET NULL"), nullable=True
    )

    members: Mapped[list[CouncilMember]] = relationship(
        back_populates="council", cascade="all, delete-orphan", order_by="CouncilMember.position"
    )


class CouncilMember(IdMixin, TimestampMixin, Base):
    """A seat on a council: one agent with a role, a model and a persona."""

    __tablename__ = "council_members"

    council_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("councils.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(40), default="mock")
    model: Mapped[str] = mapped_column(String(120), default="mock-1")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.3)
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)

    council: Mapped[Council] = relationship(back_populates="members")
