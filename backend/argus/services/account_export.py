"""GDPR-style account data export and erasure.

The export is a zip archive of everything a user owns. Tables are read through SQLAlchemy core
(``Base.metadata``) so feature tables added later (templates, knowledge, ...) are picked up
automatically when they carry an ``owner_id``/``user_id`` column (or a foreign key to such a
table, e.g. skill versions, knowledge chunks, webhook deliveries and the webhook outbox), and
silently skipped when the table does not exist in this deployment's schema. Secrets (encrypted
credentials, password and key hashes, webhook secrets) are never exported.

Data outside the database: the only per-user files are artifact blobs under
``<data_dir>/artifacts/<run_id>/``. Blobs referenced by an artifact row are exported with that
artifact; any other file in a run's directory is exported under ``runs/<id>/files/``. Knowledge
uploads are not kept as files (only their extracted text, in the chunks), and sandbox
workspaces are temporary. Short-lived Redis entries (rate-limit counters, idempotency replays,
cached model lists) expire on their own TTL and are neither exported nor erased.

Erasure deletes the user row (every owned table cascades), removes the run blob directories,
and scrubs the audit trail: rows of the user's own actions keep what happened but lose their
IP address and user agent, and the user's email is removed from any audit detail (for example
failed logins against the account). Skill versions the user authored elsewhere lose the author.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

import orjson
from sqlalchemy import Table, delete, func, inspect, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from argus.core.errors import ArgusError
from argus.core.logging import get_logger
from argus.db.base import Base
from argus.db.models import AuditEvent, Run, RunEvent, SkillVersion, User
from argus.runtime.events import TERMINAL_STATUSES
from argus.services.retention import descendant_run_ids, remove_blob_dirs

log = get_logger(__name__)

EXPORT_FORMAT = 1
_CHUNK = 64 * 1024
# Tables exported with dedicated layouts (or deliberately not at all).
_SPECIAL = {
    "users",
    "runs",
    "run_events",
    "artifacts",
    "audit_events",
    "council_members",
}
# cli_credentials is exported like provider_configs: which CLI has a key and its masked hint, never
# the encrypted key (``_secret`` drops ``*_enc``). Vendor CLI logins live only in the runner volume
# and are never part of the database or an export.
_OWNER_COLUMNS = ("owner_id", "user_id")


class ExportTooLarge(ArgusError):
    status_code = 413
    code = "export_too_large"


def _secret(column: str) -> bool:
    return (
        column.endswith("_enc")
        or column in {"password_hash", "key_hash", "totp_recovery"}
        or "secret" in column
    )


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID | Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, default=_default, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)


def _clean(row: Any) -> dict[str, Any]:
    return {k: v for k, v in dict(row).items() if not _secret(k)}


def _safe_name(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or "unnamed"


def _run_files(root: Path, run_id: Any, referenced: set[str]) -> list[tuple[str, Path]]:
    """Regular files under ``root/<run_id>/`` that no artifact row references, as (relative name, path).

    Symlinks and anything resolving outside the run directory are skipped.
    """
    root = root.resolve()
    base = (root / str(run_id)).resolve()
    if root not in base.parents or not base.is_dir():
        return []
    out = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        real = path.resolve()
        if base not in real.parents:
            continue
        rel = real.relative_to(root).as_posix()
        if rel not in referenced:
            out.append((real.relative_to(base).as_posix(), real))
    return out


class _CappedZip:
    """A zip on a temp file that refuses to grow past ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.file: IO[bytes] = tempfile.TemporaryFile()
        self.zip = zipfile.ZipFile(self.file, "w", compression=zipfile.ZIP_DEFLATED)

    def _check(self) -> None:
        if self.file.tell() > self.limit:
            mib = self.limit / (1024 * 1024)
            raise ExportTooLarge(
                f"The export exceeds the {mib:.1f} MiB limit; delete old runs or ask an admin"
            )

    def write(self, name: str, data: bytes) -> None:
        self.zip.writestr(name, data)
        self._check()

    def write_file(self, name: str, src: Path) -> None:
        self.zip.write(src, name)
        self._check()

    def write_blob(self, name: str, root: Path, rel: str) -> bool:
        """Copy a blob from under ``root`` into the archive; False if it is missing or escapes root."""
        root = root.resolve()
        src = (root / rel).resolve()
        if root not in src.parents or not src.is_file():
            return False
        self.zip.write(src, name)
        self._check()
        return True

    def close(self) -> IO[bytes]:
        self.zip.close()
        self.file.seek(0)
        return self.file

    def discard(self) -> None:
        try:
            self.zip.close()
        except Exception:  # noqa: S110 - best effort
            pass
        self.file.close()


async def _owned_tables(s: AsyncSession) -> tuple[list[tuple[Table, str, str | None]], list[str]]:
    """Exportable tables that exist in the database, plus the ones that do not.

    Each entry is (table, column, parent): ``parent`` is None when ``column`` holds the owner's id,
    otherwise ``column`` is a foreign key to the ``id`` of an exported ``parent`` table (e.g.
    skill versions, knowledge documents and chunks). ``sorted_tables`` lists parents first.
    """
    from argus.db import models  # noqa: F401  (register tables)

    conn = await s.connection()
    present = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    out: list[tuple[Table, str, str | None]] = []
    missing: list[str] = []
    exported: set[str] = set()
    for table in Base.metadata.sorted_tables:
        if table.name in _SPECIAL:
            continue
        if table.name not in present:
            missing.append(table.name)
            continue
        col = next((c for c in _OWNER_COLUMNS if c in table.c), None)
        if col is not None:
            out.append((table, col, None))
            exported.add(table.name)
            continue
        fk = next(
            (f for f in table.foreign_keys if f.column.table.name in exported and f.column.name == "id"),
            None,
        )
        if fk is not None:
            out.append((table, fk.parent.name, fk.column.table.name))
            exported.add(table.name)
    return out, missing


async def _rows(s: AsyncSession, stmt) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    return [_clean(r) for r in (await s.execute(stmt)).mappings()]


async def build_export(s: AsyncSession, user: User, blob_root: Path, *, limit: int) -> tuple[IO[bytes], dict]:
    """Write the user's data into a temporary zip. Returns (file positioned at 0, manifest)."""
    z = _CappedZip(limit)
    try:
        manifest = await _fill(s, z, user, blob_root)
        await asyncio.to_thread(z.write, "manifest.json", _dumps(manifest))
        return await asyncio.to_thread(z.close), manifest
    except BaseException:
        z.discard()
        raise


async def _fill(s: AsyncSession, z: _CappedZip, user: User, blob_root: Path) -> dict[str, Any]:
    uid = user.id
    users = Base.metadata.tables["users"]
    profile = (await _rows(s, select(users).where(users.c.id == uid)))[0]
    z.write("account.json", _dumps(profile))
    counts: dict[str, int] = {}
    tables, skipped = await _owned_tables(s)

    # Every other table the user owns (councils, skills, providers, connections, ledger, webhooks,
    # api key metadata, and any feature tables such as templates or knowledge).
    ids_by_table: dict[str, list[Any]] = {}
    for table, col, parent in tables:
        if parent is None:
            rows = await _rows(s, select(table).where(table.c[col] == uid))
        else:
            rows = []
            parent_ids = ids_by_table.get(parent, [])
            for i in range(0, len(parent_ids), 500):
                rows += await _rows(s, select(table).where(table.c[col].in_(parent_ids[i : i + 500])))
        if "id" in table.c:
            ids_by_table[table.name] = [r["id"] for r in rows]
        if table.name == "councils" and rows:
            members = Base.metadata.tables["council_members"]
            ids = [r["id"] for r in rows]
            seats = await _rows(s, select(members).where(members.c.council_id.in_(ids)))
            by_council: dict[Any, list[dict[str, Any]]] = {}
            for m in seats:
                by_council.setdefault(m["council_id"], []).append(m)
            for r in rows:
                r["members"] = sorted(by_council.get(r["id"], []), key=lambda m: m.get("position", 0))
        counts[table.name] = len(rows)
        z.write(f"{table.name}.json", _dumps(rows))

    # Audit trail of the user's own actions.
    audit_rows = await _rows(
        s, select(AuditEvent.__table__).where(AuditEvent.actor_id == uid).order_by(AuditEvent.id)
    )
    counts["audit_events"] = len(audit_rows)
    z.write("audit_events.json", _dumps(audit_rows))

    # Runs: one folder each, with run.json, result.md, events.jsonl and artifacts/.
    runs_t = Run.__table__
    artifacts_t = Base.metadata.tables["artifacts"]
    run_rows = await _rows(s, select(runs_t).where(runs_t.c.owner_id == uid).order_by(runs_t.c.created_at))
    counts["runs"] = len(run_rows)
    counts["run_events"] = counts["artifacts"] = counts["run_files"] = 0
    index = []
    for run in run_rows:
        rid = run["id"]
        base = f"runs/{rid}"
        index.append(
            {
                "id": rid,
                "parent_run_id": run.get("parent_run_id"),
                "topic": run.get("topic"),
                "status": run.get("status"),
                "path": base,
            }
        )
        z.write(f"{base}/run.json", _dumps(run))
        if run.get("result"):
            z.write(f"{base}/result.md", str(run["result"]).encode())
        lines: list[bytes] = []
        stream = await s.stream(
            select(RunEvent.__table__).where(RunEvent.run_id == rid).order_by(RunEvent.seq)
        )
        async for ev in stream.mappings():
            lines.append(orjson.dumps(dict(ev), default=_default))
        counts["run_events"] += len(lines)
        if lines:
            z.write(f"{base}/events.jsonl", b"\n".join(lines) + b"\n")
        arts = await _rows(
            s,
            select(artifacts_t)
            .where(artifacts_t.c.run_id == rid)
            .order_by(artifacts_t.c.path, artifacts_t.c.version),
        )
        meta = []
        for a in arts:
            name = f"{base}/artifacts/v{a['version']}/{_safe_name(a['path'])}"
            entry = {k: v for k, v in a.items() if k != "content"}
            if a.get("content") is not None:
                z.write(name, str(a["content"]).encode())
                entry["file"] = name
            elif a.get("blob_path"):
                # None when the blob is missing on disk.
                ok = await asyncio.to_thread(z.write_blob, name, blob_root, a["blob_path"])
                entry["file"] = name if ok else None
            meta.append(entry)
        counts["artifacts"] += len(arts)
        if meta:
            z.write(f"{base}/artifacts.json", _dumps(meta))
        # Files on disk for this run that no artifact row points at.
        referenced = {str(a["blob_path"]) for a in arts if a.get("blob_path")}
        for rel, path in await asyncio.to_thread(_run_files, blob_root, rid, referenced):
            await asyncio.to_thread(z.write_file, f"{base}/files/{_safe_name(rel)}", path)
            counts["run_files"] += 1
    z.write("runs/index.json", _dumps(index))

    return {
        "format": EXPORT_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "user_id": str(uid),
        "email": user.email,
        "counts": counts,
        "skipped_tables": skipped,
        "redacted": "Encrypted credentials, password/API-key hashes and webhook secrets are not exported.",
    }


async def iter_file(f: IO[bytes]) -> AsyncIterator[bytes]:
    try:
        while True:
            chunk = await asyncio.to_thread(f.read, _CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        f.close()


def file_size(f: IO[bytes]) -> int:
    pos = f.tell()
    f.seek(0, 2)
    size = f.tell()
    f.seek(pos)
    return size


# ---------------------------------------------------------------- erasure
async def active_runs(s: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await s.execute(
        select(Run.id).where(Run.owner_id == user_id, Run.status.not_in(TERMINAL_STATUSES))
    )
    return list(rows.scalars())


async def other_active_admins(s: AsyncSession, user_id: uuid.UUID) -> int:
    q = select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
    return int((await s.execute(q)).scalar_one())


async def delete_account(s: AsyncSession, user: User, blob_root: Path) -> dict[str, int]:
    """Delete the user and everything they own (FK cascades), then their artifact blobs.

    Commits the transaction itself so blobs are only removed once the rows are gone.
    """
    roots = list((await s.execute(select(Run.id).where(Run.owner_id == user.id))).scalars())
    run_ids = await descendant_run_ids(s, roots)
    await _scrub_references(s, user)
    # Sub-runs of the user's runs are owned by the same user; anything else stays.
    await s.execute(delete(User).where(User.id == user.id))
    await s.commit()
    dirs, size = await asyncio.to_thread(remove_blob_dirs, blob_root, run_ids)
    return {"runs": len(run_ids), "blob_dirs": dirs, "blob_bytes": size}


async def _scrub_references(s: AsyncSession, user: User) -> None:
    """Remove personal data that survives the cascade in rows the user does not own."""
    # Audit rows keep the action and time; the FK sets actor_id to NULL on delete.
    await s.execute(update(AuditEvent).where(AuditEvent.actor_id == user.id).values(ip="", user_agent=""))
    email = AuditEvent.detail["email"].as_string()
    rows = (
        await s.execute(
            select(AuditEvent.id, AuditEvent.detail).where(
                or_(email == user.email, email == user.email.lower())
            )
        )
    ).all()
    for row_id, detail in rows:
        clean = {k: v for k, v in (detail or {}).items() if k != "email"}
        await s.execute(
            update(AuditEvent).where(AuditEvent.id == row_id).values(detail={**clean, "email": None})
        )
    await s.execute(
        update(SkillVersion)
        .where(or_(SkillVersion.author_id == user.id, SkillVersion.author == user.email))
        .values(author="")
    )
