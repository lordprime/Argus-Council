"""CLI agents: per-user access to the vendor CLIs in the agent runner.

Isolation rules enforced here (and again in the runner):

* A CLI provider is built per (user, CLI) and always runs with that user's id, so the runner
  uses that user's HOME (their own CLI login) and that user's own API key. The user comes from
  the run's owner, never from seat config or request data, so nobody, admins included, can run
  another user's CLI account or resume another user's session.
* API keys are encrypted at rest, decrypted only to start that user's process, never returned by
  the API, logged, or exported.
* The whole feature is off unless ``ARGUS_CLI_AGENTS_ENABLED=true``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.core.config import Settings
from argus.core.crypto import SecretBox
from argus.core.errors import ArgusError
from argus.core.logging import get_logger
from argus.db.base import utcnow
from argus.db.models import CliCredential, CliSession
from argus.providers.cli.client import RunnerTransport, RunnerUnavailable, build_runner
from argus.providers.cli.errors import CLIError
from argus.providers.cli.provider import CLI_PROVIDERS, SUGGESTED_MODELS, CLIProvider, cli_for_provider
from argus.providers.health import key_hint
from argus.runner.specs import CLI_NAMES, LOGIN_COMMANDS

log = get_logger(__name__)

CLI_LABELS = {"claude": "Claude Code", "codex": "OpenAI Codex CLI", "gemini": "Gemini CLI"}
CLI_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY", "gemini": "GEMINI_API_KEY"}
POLICY_NOTICE = (
    "Each vendor's terms govern how its CLI may be used. Subscription logins (Claude Pro/Max, ChatGPT "
    "Plus/Pro, a Google account) are for the account owner's own use; on an instance shared by several "
    "people, use API keys. Argus never sees or stores your vendor login: the CLI signs in by itself inside "
    "the runner, in your own runner home."
)


class CLIAgentsDisabled(ArgusError):
    status_code = 403
    code = "cli_agents_disabled"


class CLIAgents:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker | None,
        transport: RunnerTransport | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.box = SecretBox(settings)
        self.transport: RunnerTransport | None = (
            transport
            if transport is not None
            else (build_runner(settings) if settings.cli_agents_enabled else None)
        )
        self.allow_subscription = settings.cli_agents_allow_subscription
        self.timeout_s = settings.cli_agents_timeout_s
        self._providers: dict[tuple[uuid.UUID, str], CLIProvider] = {}

    # ------------------------------------------------------------------ gate
    @property
    def enabled(self) -> bool:
        return bool(self.settings.cli_agents_enabled and self.transport is not None)

    def allowed(self, cli: str) -> bool:
        return self.enabled and cli in self.settings.cli_agents_allowed

    def require_enabled(self) -> None:
        if not self.enabled:
            raise CLIAgentsDisabled("CLI agents are disabled on this instance (ARGUS_CLI_AGENTS_ENABLED)")

    # ------------------------------------------------------------------ providers
    def provider_for(self, owner_id: uuid.UUID | None, name: str) -> CLIProvider:
        cli = cli_for_provider(name)
        if cli is None:
            raise ValueError(f"not a CLI provider: {name}")
        if not self.allowed(cli):
            # Reported as "unavailable" so a seat's fallback route (e.g. the API-key provider) takes over.
            raise CLIError(f"{name} is disabled on this instance", provider=name, category="unavailable")
        if owner_id is None:
            raise CLIError(f"{name} needs a signed-in user", provider=name, category="auth")
        key = (owner_id, cli)
        if key not in self._providers:
            self._providers[key] = CLIProvider(cli, owner_id, self)
        return self._providers[key]

    def forget_run(self, run_id: uuid.UUID | str) -> None:
        for provider in self._providers.values():
            provider.forget_run(str(run_id))

    def available_names(self) -> list[str]:
        return [name for name, cli in CLI_PROVIDERS.items() if self.allowed(cli)]

    # ------------------------------------------------------------------ CLIBackend
    async def api_key(self, owner_id: uuid.UUID, cli: str) -> str | None:
        if self.sessionmaker is None:
            return None
        async with self.sessionmaker() as s:
            row = (
                await s.execute(
                    select(CliCredential).where(CliCredential.owner_id == owner_id, CliCredential.cli == cli)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        try:
            return self.box.decrypt(row.api_key_enc)
        except ValueError:
            log.warning("cli_agents.key_undecryptable", cli=cli)
            return None

    async def session_owned(self, owner_id: uuid.UUID, cli: str, session_id: str) -> bool:
        if self.sessionmaker is None:
            return False
        async with self.sessionmaker() as s:
            row = (
                await s.execute(
                    select(CliSession.id).where(
                        CliSession.owner_id == owner_id,
                        CliSession.cli == cli,
                        CliSession.session_id == session_id.lower(),
                    )
                )
            ).first()
        return row is not None

    async def remember_session(
        self, owner_id: uuid.UUID, cli: str, session_id: str, run_id: str | None, member_id: str | None
    ) -> None:
        if self.sessionmaker is None:
            return
        try:
            run_uuid = uuid.UUID(run_id) if run_id else None
        except ValueError:
            run_uuid = None
        async with self.sessionmaker() as s:
            row = (
                await s.execute(
                    select(CliSession).where(
                        CliSession.owner_id == owner_id,
                        CliSession.cli == cli,
                        CliSession.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                s.add(
                    CliSession(
                        owner_id=owner_id,
                        cli=cli,
                        session_id=session_id,
                        run_id=run_uuid,
                        member_id=(member_id or "")[:64] or None,
                        label="",
                    )
                )
            else:
                row.last_used_at = utcnow()
            await s.commit()

    # ------------------------------------------------------------------ management (API)
    async def set_key(self, db: AsyncSession, owner_id: uuid.UUID, cli: str, api_key: str) -> CliCredential:
        row = (
            await db.execute(
                select(CliCredential).where(CliCredential.owner_id == owner_id, CliCredential.cli == cli)
            )
        ).scalar_one_or_none()
        if row is None:
            row = CliCredential(owner_id=owner_id, cli=cli, api_key_enc="")
            db.add(row)
        row.api_key_enc = self.box.encrypt(api_key)
        row.key_hint = key_hint(api_key)
        await db.flush()
        return row

    async def delete_key(self, db: AsyncSession, owner_id: uuid.UUID, cli: str) -> bool:
        row = (
            await db.execute(
                select(CliCredential).where(CliCredential.owner_id == owner_id, CliCredential.cli == cli)
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        await db.flush()
        return True

    async def overview(self, db: AsyncSession, owner_id: uuid.UUID, *, probe: bool = True) -> dict[str, Any]:
        keys = {
            r.cli: r.key_hint
            for r in (
                await db.execute(select(CliCredential).where(CliCredential.owner_id == owner_id))
            ).scalars()
        }
        versions: dict[str, str | None] = {}
        runner_error: str | None = None
        if self.enabled and probe:
            assert self.transport is not None
            try:
                versions = await self.transport.versions()
            except RunnerUnavailable as exc:
                runner_error = str(exc)
        clis = []
        for cli in CLI_NAMES:
            status: dict[str, Any] = {"logged_in": None, "method": None, "verified": False, "detail": ""}
            if self.allowed(cli) and probe and runner_error is None:
                assert self.transport is not None
                try:
                    status = await self.transport.status(str(owner_id), cli)
                except RunnerUnavailable as exc:
                    status["detail"] = str(exc)
            clis.append(
                {
                    "cli": cli,
                    "provider": f"{cli}-cli",
                    "label": CLI_LABELS[cli],
                    "enabled": self.allowed(cli),
                    "version": versions.get(cli),
                    "installed": bool(versions.get(cli)) if versions else None,
                    "logged_in": status.get("logged_in"),
                    "login_method": status.get("method"),
                    "login_verified": bool(status.get("verified")),
                    "status_detail": (status.get("detail") or "")[:300],
                    "has_api_key": cli in keys,
                    "key_hint": keys.get(cli),
                    "key_env": CLI_KEY_ENV[cli],
                    "login_command": f"./scripts/agent-login.sh <your-email> {cli}",
                    "cli_login_command": LOGIN_COMMANDS[cli],
                    "suggested_models": list(SUGGESTED_MODELS[cli]),
                }
            )
        return {
            "enabled": self.enabled,
            "allow_subscription": self.allow_subscription,
            "runner_error": runner_error,
            "notice": POLICY_NOTICE,
            "clis": clis,
        }

    async def list_sessions(
        self, db: AsyncSession, owner_id: uuid.UUID, cli: str | None = None
    ) -> list[CliSession]:
        q = select(CliSession).where(CliSession.owner_id == owner_id)
        if cli:
            q = q.where(CliSession.cli == cli)
        return list((await db.execute(q.order_by(CliSession.last_used_at.desc()).limit(200))).scalars())

    async def purge_user(self, owner_id: uuid.UUID) -> bool:
        """Remove a user's runner HOME (CLI logins and session stores). Used on account erasure."""
        if self.transport is None:
            return False
        try:
            return await self.transport.purge(str(owner_id))
        except RunnerUnavailable as exc:
            log.warning("cli_agents.purge_failed", error=str(exc)[:200])
            return False
