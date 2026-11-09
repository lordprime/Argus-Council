"""Browser sessions: one per (run, member), capped per run and across the service, reaped on idle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from argus.browser.engine import ACTIONS, ActionResult, Engine, SessionGone, build_command
from argus.browser.limits import SLOT_WAIT_S

log = logging.getLogger(__name__)

BUSY_MESSAGE = "browser busy, use web_fetch or try later"


class BrowserBusy(Exception):
    """No session slot came free in time."""


class UnknownSession(KeyError):
    """The session does not exist (never opened, closed, reaped, or lost by the engine)."""


class ActionRefused(ValueError):
    """The action is not one of the read-only actions, or its arguments are not acceptable."""


@dataclass
class _Session:
    run_id: str
    member_id: str
    last_used: float = field(default_factory=time.monotonic)
    busy: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionManager:
    def __init__(
        self,
        engine: Engine,
        *,
        max_sessions: int,
        per_run: int,
        idle_timeout_s: float,
        wait_s: float = SLOT_WAIT_S,
    ) -> None:
        self.engine = engine
        self.max_sessions = max_sessions
        self.per_run = per_run
        self.idle_timeout_s = idle_timeout_s
        self.wait_s = wait_s
        self._sessions: dict[str, _Session] = {}
        self._stopping: dict[str, str] = {}  # session id -> run id, still holding its slot
        self._cond = asyncio.Condition()

    # -- slots --

    def _find(self, run_id: str, member_id: str) -> str | None:
        for sid, s in self._sessions.items():
            if s.run_id == run_id and s.member_id == member_id:
                return sid
        return None

    def _has_slot(self, run_id: str) -> bool:
        total = len(self._sessions) + len(self._stopping)
        in_run = sum(1 for s in self._sessions.values() if s.run_id == run_id)
        in_run += sum(1 for r in self._stopping.values() if r == run_id)
        return total < self.max_sessions and in_run < self.per_run

    async def open(self, run_id: str, member_id: str, viewport: tuple[int, int] = (1280, 800)) -> str:
        """The (run, member) session, started if needed. Waits ``wait_s`` for a slot."""
        starting = False
        async with self._cond:

            def ready() -> bool:
                return self._find(run_id, member_id) is not None or self._has_slot(run_id)

            if not ready():
                try:
                    await asyncio.wait_for(self._cond.wait_for(ready), self.wait_s)
                except TimeoutError:
                    raise BrowserBusy(BUSY_MESSAGE) from None
            sid = self._find(run_id, member_id)
            if sid is None:
                sid, starting = secrets.token_hex(12), True
                self._sessions[sid] = _Session(run_id, member_id)  # holds the slot while starting
            session = self._sessions[sid]

        if starting:
            try:
                await self.engine.start(sid, viewport=viewport)
            except BaseException:
                async with self._cond:
                    self._sessions.pop(sid, None)
                    self._cond.notify_all()
                session.ready.set()
                raise
            session.ready.set()
            if self._sessions.get(sid) is not session:  # closed (run ended) while Chrome started
                await self.engine.stop(sid)
                raise UnknownSession(sid)
            session.last_used = time.monotonic()
            log.info("browser session %s opened for run %s member %s", sid, run_id, member_id)
            return sid

        await session.ready.wait()  # another caller may still be starting it
        if self._sessions.get(sid) is not session:
            raise BrowserBusy("the browser session failed to start")
        session.last_used = time.monotonic()
        return sid

    def _get(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None or not session.ready.is_set():
            raise UnknownSession(session_id)
        return session

    # -- actions --

    async def act(self, session_id: str, action: str, args: dict[str, Any]) -> ActionResult:
        if action not in ACTIONS:
            raise ActionRefused(
                f"action {action!r} is not allowed; read-only browsing offers {', '.join(ACTIONS)}"
            )
        try:
            build_command(action, args)
        except ValueError as exc:
            raise ActionRefused(str(exc)) from None
        session = self._get(session_id)
        session.busy += 1
        session.last_used = time.monotonic()
        try:
            async with session.lock:  # one action at a time per page
                if self._sessions.get(session_id) is not session:
                    raise UnknownSession(session_id)
                try:
                    return await self.engine.act(session_id, action, dict(args))
                except SessionGone:
                    log.warning("browser session %s lost by the engine", session_id)
                    await self.close(session_id)
                    raise UnknownSession(session_id) from None
        finally:
            session.busy -= 1
            session.last_used = time.monotonic()

    def frames(self, session_id: str, max_fps: int) -> AsyncIterator[bytes]:
        """The session's live JPEG frames. Raises ``UnknownSession`` now, not on first frame."""
        self._get(session_id)
        return self.engine.frames(session_id, max_fps=max_fps)

    # -- closing --

    async def close(self, session_id: str) -> None:
        async with self._cond:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            self._stopping[session_id] = session.run_id
        try:
            await self.engine.stop(session_id)
        except Exception:
            log.exception("browser session %s did not stop cleanly", session_id)
        finally:
            async with self._cond:
                self._stopping.pop(session_id, None)
                self._cond.notify_all()
        log.info("browser session %s closed", session_id)

    async def close_run(self, run_id: str) -> int:
        ids = [sid for sid, s in self._sessions.items() if s.run_id == run_id]
        await asyncio.gather(*(self.close(sid) for sid in ids))
        return len(ids)

    async def reap_idle(self) -> int:
        now = time.monotonic()
        idle = [
            sid
            for sid, s in self._sessions.items()
            if s.ready.is_set() and s.busy == 0 and now - s.last_used > self.idle_timeout_s
        ]
        for sid in idle:
            await self.close(sid)
        return len(idle)

    async def close_all(self) -> None:
        for sid in list(self._sessions):
            with contextlib.suppress(Exception):
                await self.close(sid)
