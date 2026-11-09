"""In-app sign-in: run a vendor CLI's own login command in one user's HOME and relay only what the
user has to act on.

The CLI does the whole OAuth exchange itself and stores its tokens in the user's runner HOME, as
when an operator runs ``scripts/agent-login.sh``. Argus never talks to the vendor's sign-in
endpoints and never reads the tokens. From the CLI's output the runner extracts exactly two
things: a sign-in link on the host allow-list of that CLI, and (device-code flows) the one-time
code. Raw output is never returned. A code the user pastes back is checked against a strict
pattern, written to the CLI's stdin once, and kept only in memory to scrub it from error text
until the attempt ends; it is never stored or logged.

At most one sign-in runs per (user, CLI); starting again cancels the previous one. Every attempt
has a hard deadline, after which its process group is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from argus.runner.clis import login_spec_for
from argus.runner.specs import DEVICE_CODE_RE, LOGIN_CODE_RE, Binaries, LoginSpec

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]+")
MAX_TEXT = 64 * 1024
ACTIVE = ("starting", "waiting", "verifying")
ERROR_MESSAGES = {
    "login_failed": "The CLI reported that the sign-in failed.",
    "unexpected_output": "The CLI did not show a sign-in link Argus recognises.",
    "timed_out": "The sign-in was not completed in time.",
    "not_installed": "This CLI is not installed in the runner.",
    "exited": "The CLI stopped before the sign-in finished.",
}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "\n")


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


@dataclass
class LoginAttempt:
    user_id: str
    cli: str
    spec: LoginSpec
    method: str | None
    started: float = field(default_factory=time.time)
    deadline: float = 0.0
    state: str = "starting"
    url: str | None = None
    user_code: str | None = None
    needs_code: bool = False
    retry: bool = False
    error: str | None = None
    message: str = ""
    finished_at: float | None = None
    text: str = ""
    prompts_seen: int = 0
    codes_sent: int = 0
    proc: asyncio.subprocess.Process | None = None
    master_fd: int | None = None
    task: asyncio.Task[None] | None = None
    secrets_: list[str] = field(default_factory=list)  # pasted codes, scrubbed from messages
    answered: set[int] = field(default_factory=set)  # spec.answers already sent

    def public(self) -> dict[str, Any]:
        return {
            "cli": self.cli,
            "state": self.state,
            "flow": self.spec.flow,
            "method": self.method,
            "verification_url": self.url if self.state in ACTIVE else None,
            "user_code": self.user_code if self.state in ACTIVE else None,
            "needs_code": self.needs_code and self.state in ACTIVE,
            "retry": self.retry,
            "expires_at": self.deadline,
            "error": self.error,
            "message": self.message,
        }


def none_state(cli: str) -> dict[str, Any]:
    return {
        "cli": cli,
        "state": "none",
        "flow": spec.flow if (spec := login_spec_for(cli)) else None,
        "method": None,
        "verification_url": None,
        "user_code": None,
        "needs_code": False,
        "retry": False,
        "expires_at": None,
        "error": None,
        "message": "",
    }


class LoginError(Exception):
    """A refused request (``code`` is stable, ``message`` is safe to show)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


class LoginManager:
    def __init__(
        self,
        binaries: Binaries,
        *,
        home_for: Any,
        base_env: Any,
        child_setup: Any,
        deadline_s: float = 900,
        keep_s: float = 600,
        max_per_user: int = 2,
        max_total: int = 8,
    ) -> None:
        self.binaries = binaries
        self._home_for = home_for
        self._base_env = base_env
        self._child_setup = child_setup
        self.deadline_s = deadline_s
        self.keep_s = keep_s
        self.max_per_user = max_per_user
        self.max_total = max_total
        self._attempts: dict[tuple[str, str], LoginAttempt] = {}

    # ------------------------------------------------------------------ public API
    def state(self, user_id: str, cli: str) -> dict[str, Any]:
        self._prune()
        attempt = self._attempts.get((user_id, cli))
        return attempt.public() if attempt else none_state(cli)

    async def start(self, user_id: str, cli: str, method: str | None = None) -> dict[str, Any]:
        spec = login_spec_for(cli)
        if spec is None:
            raise LoginError("not_supported", "This CLI has no sign-in Argus can run; use an API key.")
        argv = self._argv(spec, method)
        await self.cancel(user_id, cli)  # one attempt per (user, CLI): a new one replaces the old
        self._prune()
        active = [a for a in self._attempts.values() if a.state in ACTIVE]
        if len(active) >= self.max_total or sum(a.user_id == user_id for a in active) >= self.max_per_user:
            raise LoginError("busy", "Too many sign-ins in progress; finish or cancel one first.")
        home = self._home_for(user_id)
        attempt = LoginAttempt(user_id=user_id, cli=cli, spec=spec, method=method)
        attempt.deadline = attempt.started + self.deadline_s
        self._attempts[(user_id, cli)] = attempt
        try:
            self._prepare(spec, home)
            await self._spawn(attempt, argv, home)
        except FileNotFoundError:
            self._finish(attempt, "failed", "not_installed")
            return attempt.public()
        attempt.task = asyncio.create_task(self._supervise(attempt, home))
        return attempt.public()

    async def submit_code(self, user_id: str, cli: str, code: str) -> dict[str, Any]:
        code = code.strip()
        if not LOGIN_CODE_RE.match(code):
            raise ValueError("The code must be one line of letters, digits and . _ ~ # / + = -")
        attempt = self._attempts.get((user_id, cli))
        if attempt is None or attempt.state not in ACTIVE or not attempt.needs_code:
            raise LoginError("no_prompt", "The CLI is not waiting for a code.")
        attempt.secrets_.append(code)
        data = (code + ("\r" if attempt.spec.pty else "\n")).encode()
        try:
            if attempt.master_fd is not None:
                os.write(attempt.master_fd, data)
            elif attempt.proc and attempt.proc.stdin:
                attempt.proc.stdin.write(data)
                await attempt.proc.stdin.drain()
        except (OSError, BrokenPipeError, ConnectionResetError):
            raise LoginError("exited", ERROR_MESSAGES["exited"]) from None
        attempt.codes_sent += 1
        attempt.needs_code = False
        attempt.state = "verifying"
        return attempt.public()

    async def cancel(self, user_id: str, cli: str) -> dict[str, Any]:
        attempt = self._attempts.get((user_id, cli))
        if attempt is None:
            return none_state(cli)
        if attempt.state in ACTIVE:
            self._finish(attempt, "cancelled", None)
        await self._stop(attempt)
        return attempt.public()

    async def cancel_user(self, user_id: str) -> None:
        for uid, cli in list(self._attempts):
            if uid == user_id:
                await self.cancel(uid, cli)
                self._attempts.pop((uid, cli), None)

    async def close(self) -> None:
        for attempt in list(self._attempts.values()):
            if attempt.state in ACTIVE:
                self._finish(attempt, "cancelled", None)
            await self._stop(attempt)

    # ------------------------------------------------------------------ internals
    def _argv(self, spec: LoginSpec, method: str | None) -> list[str]:
        try:
            return spec.argv(self.binaries, method)
        except ValueError as exc:
            raise LoginError("invalid_method", str(exc)) from None

    def _prepare(self, spec: LoginSpec, home: Path) -> None:
        for rel in spec.reset_files:
            with contextlib.suppress(FileNotFoundError):
                (home / rel).unlink()
        if spec.settings:
            rel, extra = spec.settings
            path = home / rel
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                current = json.loads(path.read_text())
            except (OSError, ValueError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_text(json.dumps(_merge(current, json.loads(json.dumps(extra))), indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(path)

    async def _spawn(self, attempt: LoginAttempt, argv: list[str], home: Path) -> None:
        env = self._base_env(home) | {"BROWSER": "/bin/true"} | attempt.spec.env
        cwd = home / "work" / "login"
        cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not attempt.spec.pty:
            attempt.proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=self._child_setup,
            )
            return
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        # Wide enough that a long sign-in link is never wrapped by the CLI's own layout.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
        try:
            attempt.proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=self._child_setup,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        attempt.master_fd = master

    async def _supervise(self, attempt: LoginAttempt, home: Path) -> None:
        proc = attempt.proc
        assert proc is not None
        reader = asyncio.create_task(self._read(attempt))
        waiter = asyncio.ensure_future(proc.wait())
        success_path = home / attempt.spec.success_file if attempt.spec.success_file else None
        try:
            while attempt.state in ACTIVE:
                await asyncio.wait({waiter}, timeout=0.25)
                if success_path is not None and self._written_since(success_path, attempt.started):
                    self._finish(attempt, "succeeded", None)
                    break
                if proc.returncode is not None:
                    await asyncio.wait({reader}, timeout=1)  # let the reader take the last output
                    if proc.returncode == 0 and success_path is None:
                        self._finish(attempt, "succeeded", None)
                    else:
                        self._finish(attempt, "failed", "login_failed" if attempt.codes_sent else "exited")
                    break
                if time.time() >= attempt.deadline:
                    self._finish(attempt, "expired", "timed_out")
                    break
        finally:
            await self._stop(attempt)
            for task in (reader, waiter):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _read(self, attempt: LoginAttempt) -> None:
        if attempt.master_fd is not None:
            fd = attempt.master_fd
            loop = asyncio.get_running_loop()
            done: asyncio.Future[None] = loop.create_future()

            def on_readable() -> None:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    return
                except OSError:  # EIO: the CLI closed its terminal
                    data = b""
                if not data:
                    loop.remove_reader(fd)
                    if not done.done():
                        done.set_result(None)
                    return
                self._consume(attempt, data)

            loop.add_reader(fd, on_readable)
            try:
                await done
            finally:
                with contextlib.suppress(Exception):
                    loop.remove_reader(fd)
            return
        assert attempt.proc and attempt.proc.stdout
        while True:
            data = await attempt.proc.stdout.read(65536)
            if not data:
                return
            self._consume(attempt, data)

    def _consume(self, attempt: LoginAttempt, data: bytes) -> None:
        if attempt.state not in ACTIVE:
            return
        attempt.text = (attempt.text + strip_ansi(data.decode("utf-8", "replace")))[-MAX_TEXT:]
        spec = attempt.spec
        if attempt.master_fd is not None:
            for idx, (pattern, keys) in enumerate(spec.answers):
                if idx not in attempt.answered and pattern.search(attempt.text):
                    attempt.answered.add(idx)
                    with contextlib.suppress(OSError):
                        os.write(attempt.master_fd, keys.encode())
        urls = URL_RE.findall(attempt.text)
        allowed = [u.rstrip(".,;)") for u in urls if host_allowed(u.rstrip(".,;)"), spec.url_hosts)]
        if allowed:
            attempt.url = allowed[-1]
        if spec.flow == "device_code":
            without_urls = URL_RE.sub(" ", attempt.text)
            codes = DEVICE_CODE_RE.findall(without_urls)
            if codes:
                attempt.user_code = codes[-1]
        if spec.paste_prompt is not None:
            seen = len(spec.paste_prompt.findall(attempt.text))
            if seen > attempt.prompts_seen:
                attempt.retry = attempt.codes_sent > 0
                attempt.prompts_seen = seen
                attempt.needs_code = True
        ready = attempt.url is not None and (spec.flow == "paste_code" or attempt.user_code is not None)
        asks = attempt.needs_code or (spec.flow == "device_code" and attempt.user_code is not None)
        if asks and not ready:
            # The CLI wants the user to act but showed no link on its allow-list: relay nothing.
            self._finish(attempt, "failed", "unexpected_output")
            if attempt.proc is not None:
                self._kill(attempt.proc)
            return
        if ready and attempt.state == "starting":
            attempt.state = "waiting"
        if attempt.needs_code and attempt.state == "verifying":
            attempt.state = "waiting"

    def _finish(self, attempt: LoginAttempt, state: str, error: str | None) -> None:
        if attempt.state not in ACTIVE:
            return
        attempt.state = state
        attempt.error = error
        attempt.needs_code = False
        attempt.finished_at = time.time()
        if error:
            from argus.runner.process import redact

            last = ""
            if error in ("login_failed", "exited"):
                lines = [ln.strip() for ln in attempt.text.splitlines() if ln.strip()]
                last = lines[-1] if lines else ""
                prompt = attempt.spec.paste_prompt
                if prompt is not None:  # "Paste code here if prompted > Login failed: ..." on one line
                    matches = list(prompt.finditer(last))
                    if matches:
                        last = last[matches[-1].end() :].strip()
            detail = redact(last, attempt.secrets_)[:200]
            base = ERROR_MESSAGES.get(error, "The sign-in failed.")
            attempt.message = f"{base} {detail}".strip() if detail else base
        attempt.secrets_.clear()
        attempt.text = ""

    async def _stop(self, attempt: LoginAttempt) -> None:
        proc = attempt.proc
        if proc is not None and proc.returncode is None:
            self._kill(proc)
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), 5)
        if attempt.master_fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(attempt.master_fd)
            with contextlib.suppress(OSError):
                os.close(attempt.master_fd)
            attempt.master_fd = None
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        task = attempt.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    @staticmethod
    def _written_since(path: Path, since: float) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        return st.st_size > 0 and st.st_mtime >= since - 1

    def _prune(self) -> None:
        now = time.time()
        for key, attempt in list(self._attempts.items()):
            done_at = attempt.finished_at
            if attempt.state not in ACTIVE and done_at and now - done_at > self.keep_s:
                self._attempts.pop(key, None)
