"""Per-execution user isolation for the sandbox service.

Without it every command of every user runs as the sandbox server's own uid, so one run's code
can read or change another run's workspace and leave processes behind for the next one.

With ``ARGUS_SANDBOX_UID_POOL=20000-20999`` the server (started as root with only
``CAP_SETUID``, ``CAP_SETGID`` and ``CAP_CHOWN``; see deploy/docker-compose.yml) gives every
execution a uid of its own:

1. a uid is leased from the pool; no two concurrent executions share one;
2. the workspace is created by the server, then handed to that uid (mode 0700) bottom-up;
3. the command runs as that uid with no supplementary groups, umask 077 and rlimits, so the
   per-uid process limit is per execution;
4. afterwards a helper that drops to the same uid sends ``kill(-1, SIGKILL)``, which reaches
   every process of that uid and nothing else; no ``CAP_KILL`` needed;
5. the workspace is taken back (top-down chown to the server, chmod), files the uid left in
   shared scratch directories (/tmp, /dev/shm, /work) are removed, then the uid is returned.

The root server deliberately has no ``CAP_DAC_OVERRIDE``: it reaches files only by owning them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class IsolationError(RuntimeError):
    pass


class OsOps:
    """The privileged primitives, behind one object so tests can substitute them."""

    def chown(self, path: str, uid: int, gid: int) -> None:
        os.chown(path, uid, gid, follow_symlinks=False)

    def chmod(self, path: str, mode: int) -> None:
        os.chmod(path, mode)

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def drop_to(self, uid: int) -> None:
        """Become ``uid`` irreversibly (child process only). Group first: setgid needs privilege."""
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
        if os.getuid() != uid or os.geteuid() != uid:  # pragma: no cover - kernel guarantees
            raise IsolationError("could not drop privileges")

    def kill_all(self) -> None:
        # pid -1: every process this (unprivileged) caller may signal, i.e. its own uid, but not
        # the caller itself.
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def umask(self, mask: int) -> None:
        os.umask(mask)


@dataclass
class UidPool:
    """Leases distinct uids to concurrent executions."""

    first: int
    last: int
    _free: deque[int] = field(init=False)
    _cond: asyncio.Condition | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not (1000 <= self.first <= self.last):
            raise ValueError("uid pool must be a range of unprivileged uids (>= 1000)")
        self._free = deque(range(self.first, self.last + 1))

    @classmethod
    def parse(cls, spec: str) -> UidPool:
        lo, sep, hi = spec.strip().partition("-")
        if not sep:
            raise ValueError("ARGUS_SANDBOX_UID_POOL must look like 20000-20999")
        return cls(int(lo), int(hi))

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    @property
    def available(self) -> int:
        return len(self._free)

    async def acquire(self, timeout_s: float = 30.0) -> int:
        cond = self._condition()
        async with cond:
            try:
                await asyncio.wait_for(cond.wait_for(lambda: bool(self._free)), timeout_s)
            except TimeoutError as exc:
                raise IsolationError("sandbox is busy: no free execution slot") from exc
            return self._free.popleft()

    async def release(self, uid: int) -> None:
        cond = self._condition()
        async with cond:
            if self.first <= uid <= self.last and uid not in self._free:
                self._free.append(uid)
            cond.notify()


def _walk_bottom_up(root: Path) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames + dirnames:
            yield os.path.join(dirpath, name)
    yield str(root)


class Isolation:
    def __init__(
        self,
        pool: UidPool,
        ops: OsOps | None = None,
        *,
        server_uid: int | None = None,
        scratch_dirs: Iterable[str] = ("/tmp", "/dev/shm"),  # noqa: S108 - shared dirs we clean
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        self.pool = pool
        self.ops = ops or OsOps()
        self.server_uid = os.getuid() if server_uid is None else server_uid
        self.scratch_dirs = list(scratch_dirs)
        self.spawn = spawn or asyncio.create_subprocess_exec

    @classmethod
    def from_env(cls, spec: str | None, work_root: str | None) -> Isolation | None:
        if not spec:
            return None
        if os.geteuid() != 0:
            raise IsolationError("ARGUS_SANDBOX_UID_POOL needs the sandbox server to start as root")
        dirs = ["/tmp", "/dev/shm", *([work_root] if work_root else [])]  # noqa: S108
        return cls(UidPool.parse(spec), scratch_dirs=dirs)

    # ------------------------------------------------------------ workspace ownership
    def hand_over(self, root: Path, uid: int) -> None:
        """Give the freshly written workspace to ``uid``; the directory itself last, mode 0700."""
        self.ops.chmod(str(root), 0o700)
        for path in _walk_bottom_up(root):
            self.ops.chown(path, uid, uid)

    def take_back(self, path: Path) -> None:
        """Make the server own ``path`` again, top-down, so it can read and delete it.

        Each directory is chowned and opened up *before* it is listed (the server has no
        DAC override); symlinks are re-owned but never followed.
        """
        st = self.ops.lstat(str(path))
        self.ops.chown(str(path), self.server_uid, self.server_uid)
        if stat.S_ISLNK(st.st_mode):
            return
        if stat.S_ISDIR(st.st_mode):
            self.ops.chmod(str(path), 0o700)
            for entry in os.scandir(path):
                self.take_back(Path(entry.path))
        else:
            self.ops.chmod(str(path), 0o600)

    def purge_scratch(self, uid: int) -> list[str]:
        """Delete everything ``uid`` left at the top of the shared scratch directories."""
        removed = []
        for d in self.scratch_dirs:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for entry in entries:
                try:
                    st = self.ops.lstat(entry.path)
                except OSError:
                    continue
                if st.st_uid != uid:
                    continue
                self.take_back(Path(entry.path))
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        continue
                removed.append(entry.path)
        return removed

    # ------------------------------------------------------------ processes
    def preexec(self, uid: int, limits: Callable[[], None]) -> Callable[[], None]:
        """Child-side setup for the user's command: new session, limits, umask, drop to ``uid``."""

        def run() -> None:
            limits()  # setsid + rlimits, while still privileged
            self.ops.umask(0o077)
            self.ops.drop_to(uid)

        return run

    def _killer(self, uid: int) -> Callable[[], None]:
        def run() -> None:
            self.ops.drop_to(uid)
            self.ops.kill_all()

        return run

    async def reap(self, uid: int) -> None:
        """Kill every process running as ``uid`` (twice, to catch anything forked mid-sweep)."""
        for _ in range(2):
            proc = await self.spawn(
                "/bin/true",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=self._killer(uid),
            )
            await proc.wait()
