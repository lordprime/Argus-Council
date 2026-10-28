"""Execute a command against a set of files in a throwaway directory with hard resource limits.

Used by the sandbox service (inside its locked-down container) and, for
development only, in-process.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import resource
import shutil
import signal
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from argus.sandbox.isolation import Isolation, IsolationError

MAX_FILES = 500
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_OUTPUT = 64 * 1024


class SandboxError(ValueError):
    pass


def _limits(cpu_s: int, mem_mb: int) -> None:  # runs in the child before exec
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES * 8,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _safe(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise SandboxError(f"Unsafe path: {path}")
    return p


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _materialise(root: Path, files: dict[str, str]) -> dict[str, str]:
    before: dict[str, str] = {}
    for rel, content in files.items():
        safe = _safe(rel)
        target = root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode()
        target.write_bytes(data)
        before[str(safe)] = _digest(data)
    return before


def _collect(root: Path, before: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    changed: dict[str, str] = {}
    deleted = [p for p in before if not (root / p).exists()]
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") or part in ("__pycache__", "node_modules") for part in rel.split("/")):
            continue
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES or before.get(rel) == _digest(data):
            continue
        try:
            changed[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary outputs are not synced back
        if len(changed) >= MAX_FILES:
            break
    return changed, deleted


async def execute(
    files: dict[str, str],
    command: str,
    *,
    timeout_s: int = 60,
    mem_mb: int = 1024,
    work_root: str | None = None,
    isolation: Isolation | None = None,
) -> dict[str, Any]:
    """Run ``command`` against ``files``.

    With ``isolation`` (the sandbox service in production) the command runs as a uid of its own,
    leased for this execution only; see :mod:`argus.sandbox.isolation`.
    """
    if len(files) > MAX_FILES:
        raise SandboxError("Too many files")
    if sum(len(v.encode()) for v in files.values()) > MAX_TOTAL_BYTES:
        raise SandboxError("Workspace too large")
    timeout_s = max(1, min(timeout_s, 600))
    uid: int | None = None
    if isolation is not None:
        try:
            uid = await isolation.pool.acquire()
        except IsolationError as exc:
            raise SandboxError(str(exc)) from exc
    root = Path(tempfile.mkdtemp(prefix="run-", dir=work_root))
    try:
        before = await asyncio.to_thread(_materialise, root, files)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(root),
            "TMPDIR": str(root),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        limits = functools.partial(_limits, timeout_s, mem_mb)
        if isolation is not None and uid is not None:
            await isolation.reap(uid)  # nothing may still run as this uid from an earlier lease
            await asyncio.to_thread(isolation.hand_over, root, uid)
            preexec = isolation.preexec(uid, limits)
        else:
            preexec = limits
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=preexec,
        )

        async def stop_everything() -> None:
            # Isolated: every process of the execution's uid, wherever it moved (setsid, double
            # fork). Otherwise only the process group, which a child can leave.
            if isolation is not None and uid is not None:
                await isolation.reap(uid)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s + 2)
        except TimeoutError:
            timed_out = True
            await stop_everything()
            out, err = await proc.communicate()
        # Reap background children (`cmd &`) so nothing from this run outlives it.
        await stop_everything()
        if isolation is not None:
            await asyncio.to_thread(isolation.take_back, root)
        changed, deleted = await asyncio.to_thread(_collect, root, before)
        return {
            "exit_code": proc.returncode if not timed_out else -9,
            "timed_out": timed_out,
            "stdout": out[-MAX_OUTPUT:].decode("utf-8", "replace"),
            "stderr": err[-MAX_OUTPUT:].decode("utf-8", "replace"),
            "changed": changed,
            "deleted": deleted,
        }
    finally:
        try:
            if isolation is not None and uid is not None:
                await isolation.reap(uid)
                await asyncio.to_thread(isolation.take_back, root)
                await asyncio.to_thread(isolation.purge_scratch, uid)
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)
            if isolation is not None and uid is not None:
                await isolation.pool.release(uid)
