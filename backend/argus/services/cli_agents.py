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
from argus.core.errors import ArgusError, ServiceUnavailable
from argus.core.logging import get_logger
from argus.db.base import utcnow
from argus.db.models import CliCredential, CliSession
from argus.providers.cli.client import RunnerRefused, RunnerTransport, RunnerUnavailable, build_runner
from argus.providers.cli.errors import CLIError
from argus.providers.cli.provider import CLI_PROVIDERS, SUGGESTED_MODELS, CLIProvider, cli_for_provider
from argus.providers.health import key_hint
from argus.runner.clis import login_spec_for, plugin
from argus.runner.specs import CLI_NAMES, LOGIN_COMMANDS, PLUGIN_CLIS
from argus.services.features import CLI_OFF_MESSAGE

log = get_logger(__name__)

CLI_LABELS = {"claude": "Claude Code", "codex": "OpenAI Codex CLI", "gemini": "Gemini CLI"}
CLI_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY", "gemini": "GEMINI_API_KEY"}
POLICY_NOTICE = (
    "Each vendor's terms govern how its CLI may be used. Subscription logins (Claude Pro/Max, ChatGPT "
    "Plus/Pro, a Google account) are for the account owner's own use; on an instance shared by several "
    "people, use API keys. Signing in here starts the CLI's own sign-in in your private runner home: Argus "
    "shows you only the link and code the CLI prints and passes the code you paste straight to the CLI. "
    "Argus never contacts the vendor's sign-in service itself and never reads, stores or forwards your "
    "vendor login."
)


class CLIAgentsDisabled(ArgusError):
    status_code = 403
    code = "cli_agents_disabled"


class InvalidKeyProvider(ArgusError):
    status_code = 422
    code = "invalid_key_provider"


def _plugin_attr(cli: str, attr: str, default: Any) -> Any:
    if cli not in PLUGIN_CLIS:
        return default
    try:
        return getattr(plugin(cli), attr)
    except ModuleNotFoundError:
        return default


def key_providers(cli: str) -> list[str]:
    return list(_plugin_attr(cli, "key_providers", {}))


def cli_label(cli: str) -> str:
    return CLI_LABELS.get(cli) or _plugin_attr(cli, "label", cli)


def cli_key_env(cli: str) -> str:
    if cli in CLI_KEY_ENV:
        return CLI_KEY_ENV[cli]
    env = _plugin_attr(cli, "key_env", "")
    if env:
        return env
    providers = _plugin_attr(cli, "key_providers", {})
    names = sorted({k for tmpl in providers.values() for k in tmpl if k.endswith(("_KEY", "_TOKEN"))})
    return " / ".join(names) or "API key"


class CLISubscriptionDisabled(ArgusError):
    status_code = 403
    code = "cli_subscription_disabled"


class CLILoginRefused(ArgusError):
    """The runner refused a sign-in step; the status and code come from the runner."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status
        self.code = f"cli_login_{code}"


class CLIAgents:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker | None,
        transport: RunnerTransport | None = None,
        features: Any = None,
    ) -> None:
        self.settings = settings
        # argus.services.features.FeatureSwitches: the admin's in-app switch (None: always on).
        self.features = features
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
        return bool(self.settings.cli_agents_enabled and self.transport is not None and self._switched_on())

    def _switched_on(self) -> bool:
        return self.features is None or self.features.switch("cli_agents")

    def _switched_off(self) -> bool:
        """The server allows CLI agents but an admin turned them off in the app."""
        return (
            bool(self.settings.cli_agents_enabled and self.transport is not None) and not self._switched_on()
        )

    def allowed(self, cli: str) -> bool:
        return self.enabled and cli in self.settings.cli_agents_allowed

    def require_enabled(self) -> None:
        if self._switched_off():
            raise CLIAgentsDisabled(CLI_OFF_MESSAGE)
        if not self.enabled:
            raise CLIAgentsDisabled("CLI agents are disabled on this instance (ARGUS_CLI_AGENTS_ENABLED)")

    # ------------------------------------------------------------------ providers
    def provider_for(self, owner_id: uuid.UUID | None, name: str) -> CLIProvider:
        cli = cli_for_provider(name)
        if cli is None:
            raise ValueError(f"not a CLI provider: {name}")
        if not self.allowed(cli):
            # Reported as "unavailable" so a seat's fallback route (e.g. the API-key provider) takes over.
            message = CLI_OFF_MESSAGE if self._switched_off() else f"{name} is disabled on this instance"
            raise CLIError(message, provider=name, category="unavailable")
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
        return (await self.credential(owner_id, cli))[0]

    async def credential(self, owner_id: uuid.UUID, cli: str) -> tuple[str | None, str | None]:
        """The user's API key for this CLI and, for harness CLIs, the provider it belongs to."""
        # Asked on every CLI turn, before the runner is invoked: switching CLI agents off in the app
        # reaches seats of runs already in progress (their providers are cached), and "unavailable"
        # lets a seat's fallback route take over.
        if not self._switched_on():
            raise CLIError(CLI_OFF_MESSAGE, provider=f"{cli}-cli", category="unavailable")
        if self.sessionmaker is None:
            return None, None
        async with self.sessionmaker() as s:
            row = (
                await s.execute(
                    select(CliCredential).where(CliCredential.owner_id == owner_id, CliCredential.cli == cli)
                )
            ).scalar_one_or_none()
        if row is None:
            return None, None
        try:
            return self.box.decrypt(row.api_key_enc), row.provider
        except ValueError:
            log.warning("cli_agents.key_undecryptable", cli=cli)
            return None, None

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
    async def set_key(
        self, db: AsyncSession, owner_id: uuid.UUID, cli: str, api_key: str, provider: str | None = None
    ) -> CliCredential:
        providers = key_providers(cli)
        if providers and provider not in providers:
            raise InvalidKeyProvider(f"Choose which provider this key is for: {', '.join(providers)}")
        if not providers and provider:
            raise InvalidKeyProvider(f"The {cli} CLI takes a single kind of key; no provider needed")
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
        row.provider = provider or None
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
        rows = list(
            (await db.execute(select(CliCredential).where(CliCredential.owner_id == owner_id))).scalars()
        )
        keys = {r.cli: r.key_hint for r in rows}
        key_provider_of = {r.cli: r.provider for r in rows}
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
            try:
                login_spec = login_spec_for(cli)
            except ModuleNotFoundError:
                login_spec = None
            clis.append(
                {
                    "cli": cli,
                    "provider": f"{cli}-cli",
                    "label": cli_label(cli),
                    "enabled": self.allowed(cli),
                    "version": versions.get(cli),
                    "installed": bool(versions.get(cli)) if versions else None,
                    "logged_in": status.get("logged_in"),
                    "login_method": status.get("method"),
                    "login_verified": bool(status.get("verified")),
                    "status_detail": (status.get("detail") or "")[:300],
                    "has_api_key": cli in keys,
                    "key_hint": keys.get(cli),
                    "key_env": cli_key_env(cli),
                    "key_providers": key_providers(cli),
                    "key_provider": key_provider_of.get(cli),
                    "login_command": f"./scripts/agent-login.sh <your-email> {cli}",
                    "cli_login_command": LOGIN_COMMANDS.get(cli),
                    "login_flow": login_spec.flow if login_spec else None,
                    "login_methods": list(login_spec.methods) if login_spec else [],
                    "suggested_models": list(SUGGESTED_MODELS[cli]),
                }
            )
        return {
            "enabled": self.enabled,
            # The server allows CLI agents but an admin switched them off (Settings → Features).
            "turned_off": self._switched_off(),
            "allow_subscription": self.allow_subscription,
            "runner_error": runner_error,
            "notice": POLICY_NOTICE,
            "clis": clis,
        }

    # ------------------------------------------------------------------ sign-in (own user only)
    def _login_gate(self, cli: str, *, signing_in: bool) -> RunnerTransport:
        if not self.allowed(cli):
            raise CLIAgentsDisabled(f"The {cli} CLI agent is disabled on this instance")
        if signing_in and not self.allow_subscription:
            raise CLISubscriptionDisabled(
                "Account sign-in is disabled on this instance (ARGUS_CLI_AGENTS_ALLOW_SUBSCRIPTION=false); "
                "use an API key"
            )
        assert self.transport is not None
        return self.transport

    async def _login_call(self, call: Any) -> dict[str, Any]:
        try:
            return await call
        except RunnerUnavailable as exc:
            raise ServiceUnavailable(f"The agent runner is unreachable: {exc}") from None
        except RunnerRefused as exc:
            raise CLILoginRefused(exc.status, exc.code, exc.message) from None

    async def login_start(self, owner_id: uuid.UUID, cli: str, method: str | None) -> dict[str, Any]:
        transport = self._login_gate(cli, signing_in=True)
        return await self._login_call(transport.login_start(str(owner_id), cli, method))

    async def login_state(self, owner_id: uuid.UUID, cli: str) -> dict[str, Any]:
        transport = self._login_gate(cli, signing_in=False)
        return await self._login_call(transport.login_state(str(owner_id), cli))

    async def login_code(self, owner_id: uuid.UUID, cli: str, code: str) -> dict[str, Any]:
        transport = self._login_gate(cli, signing_in=True)
        return await self._login_call(transport.login_code(str(owner_id), cli, code))

    async def login_cancel(self, owner_id: uuid.UUID, cli: str) -> dict[str, Any]:
        transport = self._login_gate(cli, signing_in=False)
        return await self._login_call(transport.login_cancel(str(owner_id), cli))

    async def logout(self, owner_id: uuid.UUID, cli: str) -> dict[str, Any]:
        """Sign the CLI out (always allowed, so a user can remove a login even with sign-in off)."""
        transport = self._login_gate(cli, signing_in=False)
        return await self._login_call(transport.logout(str(owner_id), cli))

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
