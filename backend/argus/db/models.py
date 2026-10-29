"""ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import ColumnElement

from argus.db.base import Base, IdMixin, TimestampMixin, utcnow

USER_ROLES = ("admin", "member", "viewer")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    # "admin" | "member" | "viewer" (see USER_ROLES). Source of truth for authorization.
    role: Mapped[str] = mapped_column(String(16), default="member", server_default="member", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional TOTP second factor (argus/core/totp.py): the shared secret is encrypted with SecretBox
    # and only counts once ``totp_enabled`` is set by a verified code; recovery codes are SHA-256.
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    totp_recovery: Mapped[list[Any]] = mapped_column(default=list)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def mfa_enabled(self) -> bool:
        return bool(self.totp_enabled)

    @hybrid_property
    def is_admin(self) -> bool:
        """Compatibility view of `role`; assigning True promotes, False demotes an admin to member."""
        return self.role == "admin"

    @is_admin.inplace.setter
    def _is_admin_setter(self, value: bool) -> None:
        if value:
            self.role = "admin"
        elif self.role == "admin":
            self.role = "member"

    @is_admin.inplace.expression
    @classmethod
    def _is_admin_expression(cls) -> ColumnElement[bool]:
        return cls.role == "admin"


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


class ProviderConfig(IdMixin, TimestampMixin, Base):
    """A user's named connection to an LLM backend. API keys are encrypted at rest."""

    __tablename__ = "provider_configs"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40))
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Masked, display-only fragment of the API key (e.g. "sk-…abcd"); never the key itself.
    key_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Last known connection health: unknown|ok|unreachable|auth_failed|rate_limited|error.
    health_status: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown")
    health_detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Run(IdMixin, TimestampMixin, Base):
    """One autonomous council session working on a topic."""

    __tablename__ = "runs"
    # Serves the run list and insights (owner, top-level, newest first, id as tie-breaker) and any
    # owner_id lookup, so owner_id needs no index of its own.
    __table_args__ = (Index("ix_runs_owner_parent_created", "owner_id", "parent_run_id", "created_at", "id"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    council_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("councils.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    topic: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(32), default="general")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    outputs: Mapped[list[Any]] = mapped_column(default=list)
    guardrails: Mapped[dict[str, Any]] = mapped_column(default=dict)
    council_snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Archived runs leave the default run list but stay intact; trashed runs are soft-deleted and
    # purged by retention after ``retention_trash_days``. Restoring from the trash keeps archived_at.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # Knowledge bases the user linked to this run (before it started or while it runs); agents are
    # told about them and knowledge_search may always read them. Ids as strings.
    knowledge_base_ids: Mapped[list[Any]] = mapped_column(default=list)


class RunEvent(Base):
    """Append-only, durable event log for a run (ephemeral deltas are not stored)."""

    __tablename__ = "run_events"
    # The unique (run_id, seq) index serves every lookup by run; no separate run_id or type index
    # (the hottest table, so each extra index is write cost on every durable event).
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[str] = mapped_column(String(40))
    type: Mapped[str] = mapped_column(String(40))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Artifact(IdMixin, TimestampMixin, Base):
    """A file or document produced by a run. Every write creates a new version row."""

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "path", "version"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(20), default="file")
    mime: Mapped[str] = mapped_column(String(120), default="text/plain")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(80), default="")
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Skill(IdMixin, TimestampMixin, Base):
    """A user-installed or user-authored skill package."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(40), default="1.0.0")
    body: Mapped[str] = mapped_column(Text)
    allowed_tools: Mapped[list[Any]] = mapped_column(default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    files: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="created")


class SkillVersion(IdMixin, Base):
    """Immutable snapshot of a user skill, written on every create/update/restore/install."""

    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "number"),)

    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    skill_md: Mapped[str] = mapped_column(Text)
    files: Mapped[dict[str, Any]] = mapped_column(default=dict)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author: Mapped[str] = mapped_column(String(320), default="")
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class McpServer(IdMixin, TimestampMixin, Base):
    """An external MCP server whose tools council agents may use."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    transport: Mapped[str] = mapped_column(String(10), default="http")  # http | stdio
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    command: Mapped[list[Any]] = mapped_column(default=list)  # stdio only (admin)
    headers_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted JSON of extra headers
    tool_allow: Mapped[list[Any]] = mapped_column(default=lambda: ["*"])
    risk: Mapped[list[Any]] = mapped_column(default=lambda: ["network"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_tools: Mapped[list[Any]] = mapped_column(default=list)


class RemoteAgent(IdMixin, TimestampMixin, Base):
    """A remote agent reachable over A2A or ACP that councils may delegate to."""

    __tablename__ = "remote_agents"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    protocol: Mapped[str] = mapped_column(String(8))  # a2a | acp
    url: Mapped[str] = mapped_column(String(500))
    agent_name: Mapped[str | None] = mapped_column(String(120), nullable=True)  # ACP agent name
    token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    card: Mapped[dict[str, Any]] = mapped_column(default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class LedgerEntry(Base):
    """Tamper-evident record of a cross-protocol interaction (hash-chained per owner)."""

    __tablename__ = "ledger_entries"
    __table_args__ = (UniqueConstraint("owner_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    protocol: Mapped[str] = mapped_column(String(8))  # mcp | a2a | acp
    direction: Mapped[str] = mapped_column(String(8))  # inbound | outbound
    peer: Mapped[str] = mapped_column(String(500))
    method: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16))
    request_sha256: Mapped[str] = mapped_column(String(64))
    response_sha256: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditEvent(Base):
    """Who did what, from where. Written in its own transaction so failed attempts are kept."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(120), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Webhook(IdMixin, TimestampMixin, Base):
    """An outbound HTTPS endpoint notified of run events, signed with a per-hook secret."""

    __tablename__ = "webhooks"

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list[Any]] = mapped_column(default=list)
    secret_enc: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[str] = mapped_column(String(40), default="")
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webhooks.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event: Mapped[str] = mapped_column(String(60))
    delivery_id: Mapped[str] = mapped_column(String(40))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(String(300), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class WebhookOutbox(Base):
    """A webhook delivery not yet final: waiting for its first attempt, a retry, or a lease to expire.

    Rows survive restarts and are removed once the delivery succeeds or gives up (the attempts are
    logged in ``webhook_deliveries``). ``attempts`` doubles as an optimistic lock when claiming.
    """

    __tablename__ = "webhook_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webhooks.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(60))
    delivery_id: Mapped[str] = mapped_column(String(40))
    body: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunTemplate(IdMixin, TimestampMixin, Base):
    """A saved run configuration whose topic/description may contain ``{{variables}}``."""

    __tablename__ = "run_templates"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(String(500), default="")
    topic: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(32), default="general")
    council_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("councils.id", ondelete="SET NULL"), nullable=True
    )
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(64), default="mock")
    model: Mapped[str] = mapped_column(String(120), default="mock-1")
    outputs: Mapped[list[Any]] = mapped_column(default=list)
    guardrails: Mapped[dict[str, Any]] = mapped_column(default=dict)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(IdMixin, TimestampMixin, Base):
    """An in-app notification for a user (run finished, approval needed)."""

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class KnowledgeBase(IdMixin, TimestampMixin, Base):
    """A named, owner-scoped collection of documents that agents can search."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    # "user": made and filled by the user. "library": the owner's research library, filled
    # automatically with every source agents read (one per owner, see argus.knowledge.service).
    kind: Mapped[str] = mapped_column(String(16), default="user", server_default="user")


class KnowledgeDocument(IdMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300), default="", server_default="")
    origin: Mapped[str] = mapped_column(
        String(16), default="upload", server_default="upload"
    )  # upload|text|url|agent
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    url_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )


class KnowledgeChunk(Base):
    """A searchable slice of a document; ``terms`` holds pre-computed term frequencies for BM25."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    length: Mapped[int] = mapped_column(Integer, default=0)
    terms: Mapped[dict[str, Any]] = mapped_column(default=dict)


class MemoryItem(IdMixin, TimestampMixin, Base):
    """A long-term note an agent (or user) keeps across runs, scoped to a council when set."""

    __tablename__ = "memory_items"

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    council_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("councils.id", ondelete="CASCADE"), nullable=True, index=True
    )
    key: Mapped[str] = mapped_column(String(120), default="")
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(120), default="")


class CliCredential(IdMixin, TimestampMixin, Base):
    """A user's API key for one vendor CLI (claude, codex, gemini), encrypted at rest.

    Injected only into that user's own CLI processes in the agent runner; never returned by the
    API and never included in account exports.
    """

    __tablename__ = "cli_credentials"
    __table_args__ = (UniqueConstraint("owner_id", "cli"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cli: Mapped[str] = mapped_column(String(16))
    api_key_enc: Mapped[str] = mapped_column(Text)
    key_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CliSession(IdMixin, TimestampMixin, Base):
    """A vendor CLI session a user's council seat ran in (resumable by that user only)."""

    __tablename__ = "cli_sessions"
    __table_args__ = (UniqueConstraint("owner_id", "cli", "session_id"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cli: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    member_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
