"""Run one vendor CLI turn as a subprocess, confined to one user's HOME on the runner volume.

Layout on the runner volume (``ARGUS_RUNNER_HOME_ROOT``, default ``/home/runner/users``)::

    <root>/<user_id>/                 HOME for every CLI this user runs (0700)
        .claude/ .codex/ .gemini/     each CLI's own login state and session store
        .argus/sessions.json          sessions Argus created for this user (resume allow-list)
        .argus/prompts/               per-turn system prompt files (deleted after the turn)
        work/<run_key>/               empty working directory per run seat

Only the user's own HOME is ever passed to a process, so one user's CLI login is never used for
another user's run. Nothing here reads the CLIs' credential files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import resource
import secrets
import shutil
import signal
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argus.runner.specs import (
    CLI_NAMES,
    UUID_RE,
    Binaries,
    InvokeSpec,
    build_command,
    status_argv,
    version_argv,
)

MAX_LINE_BYTES = 4 * 1024 * 1024
STDERR_TAIL = 4000
_PASSTHROUGH_ENV = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
)
_SECRET_RES = [
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"ya29\.[0-9A-Za-z_\-.]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)([\"'=:\s]+)[A-Za-z0-9._\-]{12,}"),
]


class RunnerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def redact(text: str, secrets_: list[str] | None = None) -> str:
    for value in secrets_ or []:
        if value:
            text = text.replace(value, "[REDACTED]")
    for pat in _SECRET_RES:
        text = pat.sub(
            lambda m: m.group(1) + m.group(2) + "[REDACTED]" if m.lastindex else "[REDACTED]", text
        )
    return text


@dataclass
class RunnerConfig:
    home_root: Path = Path("/home/runner/users")
    binaries: Binaries = field(default_factory=Binaries)
    max_concurrency: int = 8
    max_per_user: int = 4
    max_output_bytes: int = 32 * 1024 * 1024


def user_home(cfg: RunnerConfig, user_id: str, *, create: bool = True) -> Path:
    if not UUID_RE.match(user_id):
        raise RunnerError("invalid_user", "user_id must be a UUID")
    root = cfg.home_root.resolve()
    home = (root / user_id.lower()).resolve()
    if home.parent != root:  # pragma: no cover - UUID_RE already rules this out
        raise RunnerError("invalid_user", "user_id escapes the home root")
    if create:
        for d in (home, home / ".argus", home / ".argus" / "prompts", home / "work"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


class SessionIndex:
    """Sessions Argus created for one user; the allow-list for resuming."""

    def __init__(self, home: Path) -> None:
        self.path = home / ".argus" / "sessions.json"

    def load(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, cli: str, session_id: str) -> dict[str, Any] | None:
        entry = self.load().get(cli, {}).get(session_id.lower())
        return entry if isinstance(entry, dict) else None

    def record(self, cli: str, session_id: str, cwd: Path) -> None:
        data = self.load()
        now = time.time()
        entry = data.setdefault(cli, {}).setdefault(session_id.lower(), {"created": now})
        entry.update({"cwd": str(cwd), "last_used": now})
        tmp = self.path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def list(self, cli: str) -> list[dict[str, Any]]:
        rows = [
            {"session_id": sid, "created": e.get("created"), "last_used": e.get("last_used")}
            for sid, e in self.load().get(cli, {}).items()
            if isinstance(e, dict)
        ]
        return sorted(rows, key=lambda r: -(r["last_used"] or 0))


def _child_setup() -> None:  # runs in the child before exec
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))


def _base_env(home: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "NO_COLOR": "1",
        # Documented opt-outs: no self-updates or background traffic from a pinned image.
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    for name in _PASSTHROUGH_ENV:
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


class Runner:
    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg
        self._slots = asyncio.Semaphore(cfg.max_concurrency)
        self._per_user: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._versions: dict[str, str | None] | None = None

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    # ------------------------------------------------------------------ invoke
    async def invoke(self, spec: InvokeSpec) -> AsyncIterator[dict[str, Any]]:
        """Yield the CLI's JSON events, then one ``argus.exit`` record."""
        home = user_home(self.cfg, spec.user_id)
        index = SessionIndex(home)
        if spec.resume:
            assert spec.session_id is not None
            entry = index.get(spec.cli, spec.session_id)
            if entry is None:
                yield {
                    "type": "argus.exit",
                    "ok": False,
                    "error": "unknown_session",
                    "detail": "No such session for this user and CLI.",
                }
                return
            cwd = self._safe_cwd(home, entry.get("cwd"))
        else:
            key = spec.run_key or f"adhoc-{secrets.token_hex(6)}"
            cwd = home / "work" / key
        cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._per_user.get(spec.user_id, 0) >= self.cfg.max_per_user:
            yield {
                "type": "argus.exit",
                "ok": False,
                "error": "busy",
                "detail": "Too many CLI turns at once.",
            }
            return
        prompt_file: Path | None = None
        if spec.system_prompt and spec.cli in ("claude", "gemini"):
            prompt_file = home / ".argus" / "prompts" / f"{secrets.token_hex(8)}.md"
            prompt_file.write_text(spec.system_prompt)
            os.chmod(prompt_file, 0o600)
        cmd = build_command(
            spec, binaries=self.cfg.binaries, system_prompt_file=str(prompt_file) if prompt_file else None
        )
        env = _base_env(home) | cmd.env
        key_values = [v for k, v in cmd.env.items() if k.endswith("_API_KEY")]
        session_id = spec.session_id
        self._per_user[spec.user_id] = self._per_user.get(spec.user_id, 0) + 1
        started = time.monotonic()
        try:
            async with self._slots:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd.argv,
                        cwd=str(cwd),
                        env=env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=MAX_LINE_BYTES,
                        preexec_fn=_child_setup,
                    )
                except FileNotFoundError:
                    yield {
                        "type": "argus.exit",
                        "ok": False,
                        "error": "not_installed",
                        "detail": f"{spec.cli} is not installed",
                    }
                    return
                assert proc.stdin and proc.stdout and proc.stderr
                stderr_task = asyncio.create_task(proc.stderr.read(256 * 1024))
                try:
                    proc.stdin.write(cmd.stdin.encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                total = 0
                timed_out = False
                eof = False
                deadline = started + spec.timeout_s
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        try:
                            line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                        except TimeoutError:
                            timed_out = True
                            break
                        except ValueError:  # one line above MAX_LINE_BYTES
                            continue
                        if not line:
                            eof = True
                            break
                        total += len(line)
                        if total > self.cfg.max_output_bytes:
                            yield {
                                "type": "argus.exit",
                                "ok": False,
                                "error": "output_too_large",
                                "detail": "CLI output exceeded the cap",
                            }
                            return
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue  # banners and warnings are not part of the protocol
                        if not isinstance(event, dict):
                            continue
                        if spec.cli == "codex" and event.get("type") == "thread.started":
                            tid = str(event.get("thread_id") or "")
                            if UUID_RE.match(tid):
                                session_id = tid.lower()
                        yield event
                finally:
                    # After EOF give the CLI a moment to exit; on timeout, an output cap or a client
                    # that went away (generator closed), kill the whole process group at once.
                    if proc.returncode is None and eof:
                        try:
                            await asyncio.wait_for(proc.wait(), 10)
                        except TimeoutError:
                            pass
                    if proc.returncode is None:
                        _kill(proc)
                        await proc.wait()
                    stderr = b""
                    with contextlib.suppress(Exception):
                        stderr = await asyncio.wait_for(stderr_task, 5)
                code = proc.returncode
                if spec.persist and session_id and code == 0:
                    async with self._lock(spec.user_id):
                        await asyncio.to_thread(index.record, spec.cli, session_id, cwd)
                tail = redact(stderr.decode("utf-8", "replace")[-STDERR_TAIL:], key_values)
                yield {
                    "type": "argus.exit",
                    "ok": code == 0 and not timed_out,
                    "exit_code": code,
                    "timed_out": timed_out,
                    "error": "timeout" if timed_out else (None if code == 0 else "exit_status"),
                    "detail": tail,
                    "session_id": session_id if spec.persist else None,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
        finally:
            self._per_user[spec.user_id] -= 1
            if prompt_file is not None:
                with contextlib.suppress(OSError):
                    prompt_file.unlink()

    def _safe_cwd(self, home: Path, recorded: Any) -> Path:
        work = (home / "work").resolve()
        if isinstance(recorded, str):
            candidate = Path(recorded).resolve()
            if candidate.parent == work:
                return candidate
        return work / "resumed"

    # ------------------------------------------------------------------ status
    async def _capture(
        self, argv: list[str], home: Path, extra_env: dict[str, str], wait_s: float = 20
    ) -> tuple[int | None, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(home),
                env=_base_env(home) | extra_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=_child_setup,
            )
        except FileNotFoundError:
            return None, "not installed"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), wait_s)
        except TimeoutError:
            _kill(proc)
            await proc.wait()
            return None, "timed out"
        return proc.returncode, out.decode("utf-8", "replace")[-4000:]

    async def versions(self, *, refresh: bool = False) -> dict[str, str | None]:
        if self._versions is not None and not refresh:
            return self._versions
        tmp_home = self.cfg.home_root / ".versions"
        tmp_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        found: dict[str, str | None] = {}
        for cli in CLI_NAMES:
            code, out = await self._capture(version_argv(cli, self.cfg.binaries), tmp_home, {})
            line = out.strip().splitlines()[-1] if out.strip() else ""
            found[cli] = line[:80] if code == 0 and line else None
        self._versions = found
        return found

    async def status(self, user_id: str, cli: str) -> dict[str, Any]:
        """Login state for one user and CLI. Never returns account details or tokens."""
        home = user_home(self.cfg, user_id)
        argv = status_argv(cli, self.cfg.binaries)
        if argv is None:  # gemini: no documented status command
            present = (home / ".gemini" / "oauth_creds.json").exists()
            return {
                "logged_in": True if present else None,
                "method": "google_login" if present else None,
                "verified": False,
                "detail": "Sign-in state found (not verified until first use)."
                if present
                else "No sign-in found.",
            }
        code, out = await self._capture(argv, home, {})
        if code is None:
            return {"logged_in": None, "method": None, "verified": False, "detail": out}
        if cli == "claude":
            try:
                data = json.loads(out[out.find("{") :])
            except ValueError:
                data = {}
            method = data.get("authMethod") if isinstance(data.get("authMethod"), str) else None
            logged_in = bool(data.get("loggedIn")) if "loggedIn" in data else code == 0
            return {
                "logged_in": logged_in,
                "method": method if logged_in else None,
                "verified": True,
                "detail": "",
            }
        # codex: "Logged in using ChatGPT" / "Logged in using an API key - sk-…" / "Not logged in"
        text = out.lower()
        method = "chatgpt" if "chatgpt" in text else ("api_key" if "api key" in text else None)
        return {
            "logged_in": code == 0,
            "method": method if code == 0 else None,
            "verified": True,
            "detail": "",
        }

    def sessions(self, user_id: str, cli: str) -> list[dict[str, Any]]:
        return SessionIndex(user_home(self.cfg, user_id)).list(cli)

    def has_session(self, user_id: str, cli: str, session_id: str) -> bool:
        return SessionIndex(user_home(self.cfg, user_id)).get(cli, session_id) is not None

    async def purge(self, user_id: str) -> bool:
        home = user_home(self.cfg, user_id, create=False)
        if not home.exists():
            return False
        await asyncio.to_thread(shutil.rmtree, home, True)
        return True
