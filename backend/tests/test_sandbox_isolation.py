"""SEC-08: per-execution uid isolation in the sandbox, tested without root or Docker.

The privileged primitives (chown, setuid, kill) are replaced by a recording fake, so these tests
check the decisions (which uid, which paths, which order) and never signal real processes. CI's
compose smoke test checks the real thing in the container.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.sandbox.isolation import Isolation, IsolationError, OsOps, UidPool


class FakeOps(OsOps):
    def __init__(self, owners: dict[str, int] | None = None) -> None:
        self.calls: list[tuple] = []
        self.owners = owners or {}

    def chown(self, path, uid, gid):
        self.calls.append(("chown", path, uid))

    def chmod(self, path, mode):
        self.calls.append(("chmod", path, mode))

    def lstat(self, path):
        st = os.lstat(path)
        uid = self.owners.get(path, st.st_uid)
        return SimpleNamespace(st_mode=st.st_mode, st_uid=uid)

    def drop_to(self, uid):
        self.calls.append(("drop_to", uid))

    def kill_all(self):
        self.calls.append(("kill_all",))

    def umask(self, mask):
        self.calls.append(("umask", mask))


class FakeProc:
    async def wait(self):
        return 0


def fake_spawn(log: list):
    async def spawn(*args, preexec_fn=None, **kw):
        log.append(args)
        preexec_fn()  # in-process with FakeOps: records, signals nothing
        return FakeProc()

    return spawn


def _iso(ops: FakeOps, pool=None, **kw) -> tuple[Isolation, list]:
    log: list = []
    iso = Isolation(pool or UidPool(20000, 20002), ops, server_uid=0, spawn=fake_spawn(log), **kw)
    return iso, log


async def test_uid_pool_leases_distinct_uids_and_waits():
    pool = UidPool(20000, 20001)
    a, b = await pool.acquire(), await pool.acquire()
    assert {a, b} == {20000, 20001} and pool.available == 0
    with pytest.raises(IsolationError, match="busy"):
        await pool.acquire(timeout_s=0.05)
    waiter = asyncio.create_task(pool.acquire(timeout_s=2))
    await asyncio.sleep(0.01)
    await pool.release(a)
    assert await waiter == a
    await pool.release(a)
    await pool.release(a)  # double release does not duplicate a uid
    assert pool.available == 1
    await pool.release(5)  # foreign uids are never added
    assert pool.available == 1


def test_uid_pool_rejects_privileged_or_malformed_ranges():
    assert (UidPool.parse("20000-20999").first, UidPool.parse("20000-20999").last) == (20000, 20999)
    for bad in ("0-10", "20000", "30000-20000"):
        with pytest.raises(ValueError):
            UidPool.parse(bad)


def test_from_env_requires_root(monkeypatch):
    assert Isolation.from_env(None, "/work") is None
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(IsolationError, match="root"):
        Isolation.from_env("20000-20999", "/work")


def test_hand_over_locks_root_and_chowns_bottom_up(tmp_path):
    ws = tmp_path / "run-1"
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "src" / "pkg" / "a.py").write_text("x")
    (ws / "README.md").write_text("y")
    ops = FakeOps()
    iso, _ = _iso(ops)
    iso.hand_over(ws, 20001)
    assert ops.calls[0] == ("chmod", str(ws), 0o700)
    chowns = [c for c in ops.calls if c[0] == "chown"]
    assert {c[2] for c in chowns} == {20001}
    paths = [c[1] for c in chowns]
    assert set(paths) == {
        str(ws),
        str(ws / "src"),
        str(ws / "src" / "pkg"),
        str(ws / "src" / "pkg" / "a.py"),
        str(ws / "README.md"),
    }
    # Children before parents, the workspace itself last (the server has no DAC override).
    assert paths[-1] == str(ws)
    assert (
        paths.index(str(ws / "src" / "pkg" / "a.py"))
        < paths.index(str(ws / "src" / "pkg"))
        < paths.index(str(ws / "src"))
    )


def test_take_back_top_down_without_following_symlinks(tmp_path):
    ws = tmp_path / "run-1"
    (ws / "d").mkdir(parents=True)
    (ws / "d" / "f.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("v")
    (ws / "link").symlink_to(outside)
    ops = FakeOps()
    iso, _ = _iso(ops)
    iso.take_back(ws)
    chowned = [c[1] for c in ops.calls if c[0] == "chown"]
    assert all(c[2] == 0 for c in ops.calls if c[0] == "chown")
    assert chowned[0] == str(ws)  # parent before children
    assert chowned.index(str(ws / "d")) < chowned.index(str(ws / "d" / "f.txt"))
    assert str(ws / "link") in chowned  # the link itself (lchown) ...
    assert not any(str(outside) in p for p in chowned)  # ... but never its target
    modes = {c[1]: c[2] for c in ops.calls if c[0] == "chmod"}
    assert modes[str(ws)] == 0o700 and modes[str(ws / "d" / "f.txt")] == 0o600
    assert str(ws / "link") not in modes


def test_preexec_applies_limits_then_drops_privileges():
    ops = FakeOps()
    iso, _ = _iso(ops)
    order: list = []
    iso.preexec(20002, lambda: order.append("limits"))()
    assert order == ["limits"]
    assert ops.calls == [("umask", 0o077), ("drop_to", 20002)]


async def test_reap_kills_as_the_execution_uid():
    ops = FakeOps()
    iso, log = _iso(ops)
    await iso.reap(20001)
    # Two sweeps, each: become the uid, then kill(-1) (reaches only that uid's processes).
    assert ops.calls == [("drop_to", 20001), ("kill_all",)] * 2
    assert all(args[0] == "/bin/true" for args in log)


def test_purge_scratch_removes_only_the_uids_leftovers(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    mine_file, mine_dir, theirs = tmp / "left.txt", tmp / "cache", tmp / "other.txt"
    mine_file.write_text("x")
    mine_dir.mkdir()
    (mine_dir / "inner").write_text("y")
    theirs.write_text("z")
    owners = {str(mine_file): 20001, str(mine_dir): 20001, str(mine_dir / "inner"): 20001, str(theirs): 20002}
    ops = FakeOps(owners)
    iso, _ = _iso(ops, scratch_dirs=[str(tmp), str(tmp_path / "missing")])
    removed = iso.purge_scratch(20001)
    assert sorted(removed) == sorted([str(mine_file), str(mine_dir)])
    assert not mine_file.exists() and not mine_dir.exists() and theirs.exists()


async def test_execute_isolated_lifecycle(tmp_path, monkeypatch):
    """The executor leases a uid, hands the workspace over, reaps, takes it back and releases."""
    from argus.sandbox.executor import execute

    ops = FakeOps()
    pool = UidPool(20000, 20000)
    iso, _ = _iso(ops, pool=pool, scratch_dirs=[])
    # Shell builtins only: forking is not needed to exercise the lifecycle.
    res = await execute(
        {"in.txt": "hello"},
        'read x < in.txt; echo "$x world" > out.txt; echo done',
        timeout_s=10,
        work_root=str(tmp_path),
        isolation=iso,
    )
    assert res["exit_code"] == 0 and res["stdout"].strip() == "done", res
    assert res["changed"] == {"out.txt": "hello world\n"}
    kinds = [c[0] for c in ops.calls]
    first_chown = kinds.index("chown")
    # Reap before the workspace is handed over (nothing left from an earlier lease) ...
    assert kinds.index("kill_all") < first_chown
    assert {c[2] for c in ops.calls[first_chown:] if c[0] == "chown"} >= {20000, 0}
    # ... and the uid is free again, the workspace gone.
    assert pool.available == 1 and list(tmp_path.iterdir()) == []


async def test_execute_isolated_releases_uid_on_failure(tmp_path):
    from argus.sandbox.executor import SandboxError, execute

    pool = UidPool(20000, 20000)
    iso, _ = _iso(FakeOps(), pool=pool, scratch_dirs=[])
    with pytest.raises(SandboxError):
        await execute({"../escape": "x"}, "true", work_root=str(tmp_path), isolation=iso)
    assert pool.available == 1 and list(tmp_path.iterdir()) == []
    # An exhausted pool is reported as a sandbox error, not a hang.
    await pool.acquire()
    monkey = pool.acquire

    async def quick(timeout_s=30.0):
        return await monkey(timeout_s=0.05)

    pool.acquire = quick  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="busy"):
        await execute({}, "true", work_root=str(tmp_path), isolation=iso)


async def test_server_fails_closed_without_isolation(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    import argus.sandbox.server as server

    monkeypatch.setattr(server, "_TOKEN", "t")
    monkeypatch.setattr(server, "_ISOLATION_ERROR", "server did not start as root")
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url="http://s") as c:
        assert (await c.get("/health")).status_code == 503
        r = await c.post("/exec", headers={"Authorization": "Bearer t"}, json={"command": "true"})
        assert r.status_code == 503


def test_secure_socket_dir_only_with_isolation(monkeypatch, tmp_path):
    import argus.sandbox.server as server

    monkeypatch.setenv("ARGUS_SANDBOX_SOCKET_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_ISOLATION", None)
    assert server._secure_socket_dir() is None  # nothing to do in the unisolated (dev) mode
    calls = []
    monkeypatch.setattr(server, "_ISOLATION", object())
    monkeypatch.setattr(server.os, "chown", lambda *a: calls.append(("chown", *a)))
    monkeypatch.setattr(server.os, "chmod", lambda *a: calls.append(("chmod", *a)))
    assert server._secure_socket_dir() is None
    assert ("chown", str(tmp_path), 0, 0) in calls and ("chmod", str(tmp_path), 0o755) in calls


def test_default_ops_kill_is_scoped_to_caller():
    """The real kill helper targets pid -1 (own uid only); never called here, just inspected."""
    import inspect

    src = inspect.getsource(OsOps.kill_all)
    assert "os.kill(-1, signal.SIGKILL)" in src
    src = inspect.getsource(OsOps.drop_to)
    assert src.index("os.setgroups(") < src.index("os.setgid(") < src.index("os.setuid(")


def test_paths_are_real(tmp_path):
    # Guard against the fake masking a broken walk: hand_over sees every real entry.
    ws = tmp_path / "w"
    ws.mkdir()
    for i in range(3):
        (ws / f"f{i}").write_text(str(i))
    ops = FakeOps()
    iso, _ = _iso(ops)
    iso.hand_over(ws, 20000)
    assert len([c for c in ops.calls if c[0] == "chown"]) == 4
    assert Path(ws).exists()
