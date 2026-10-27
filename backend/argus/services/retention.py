"""Data retention: prune old runs, run events and audit rows in small batches.

Policy (all ``ARGUS_RETENTION_*`` settings, 0 = keep forever):

* ``RUN_DAYS``     finished (terminal) top-level runs older than N days are deleted together with
                   their sub-runs, events, artifacts and the artifact blobs on disk.
* ``EVENT_DAYS``   the event log of finished runs older than N days is dropped; the run row, its
                   result and its artifacts are kept, so the deliverable stays available.
* ``AUDIT_DAYS``   audit rows older than N days are deleted (default 365).
* ``LEDGER_DAYS``  the hash-chained protocol ledger is never pruned by default. Every entry hashes
                   its predecessor and ``/connections/ledger/verify`` replays the chain from the
                   genesis hash at seq 1, so deleting any prefix makes verification report a
                   broken chain forever after. Only set it if you archive the ledger elsewhere
                   first and accept that verification of the pruned owners will fail.

Blob directories under ``<data_dir>/artifacts/<run_id>/`` whose run no longer exists (deleted by
retention, by a user or by account deletion) are swept as well.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.core.config import Settings
from argus.core.logging import get_logger
from argus.db.models import AuditEvent, LedgerEntry, Run, RunEvent
from argus.runtime.events import TERMINAL_STATUSES

log = get_logger(__name__)

# Orphaned blob directories younger than this are left alone (a run may be mid-write).
_ORPHAN_GRACE_S = 3600


@dataclass
class RetentionStats:
    dry_run: bool = False
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    runs: int = 0
    run_events: int = 0
    audit_events: int = 0
    ledger_entries: int = 0
    blob_dirs: int = 0
    blob_bytes: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetentionPolicy:
    run_days: int
    event_days: int
    audit_days: int
    ledger_days: int
    batch_size: int
    interval_s: int
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        notes = []
        if settings.retention_ledger_days <= 0:
            notes.append(
                "The protocol ledger is kept forever: it is hash-chained and verification replays it "
                "from genesis, so pruning any entry would make every later verification fail."
            )
        else:
            notes.append("Ledger pruning is enabled: /connections/ledger/verify will report a broken chain.")
        if (
            settings.retention_run_days > 0
            and 0 < settings.retention_event_days >= settings.retention_run_days
        ):
            notes.append("Event retention is not shorter than run retention, so it has no extra effect.")
        return cls(
            run_days=max(settings.retention_run_days, 0),
            event_days=max(settings.retention_event_days, 0),
            audit_days=max(settings.retention_audit_days, 0),
            ledger_days=max(settings.retention_ledger_days, 0),
            batch_size=max(settings.retention_batch_size, 1),
            interval_s=max(settings.retention_interval_s, 60),
            notes=notes,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def blob_root(settings: Settings) -> Path:
    return Path(settings.data_dir) / "artifacts"


def _cutoff(days: int, now: datetime) -> datetime | None:
    return now - timedelta(days=days) if days > 0 else None


def _finished_before(cutoff: datetime):  # type: ignore[no-untyped-def]
    return Run.status.in_(TERMINAL_STATUSES) & (func.coalesce(Run.finished_at, Run.updated_at) < cutoff)


async def descendant_run_ids(s: AsyncSession, roots: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """The given runs plus every sub-run below them (sub-runs are removed by FK cascade)."""
    seen: list[uuid.UUID] = []
    frontier = list(roots)
    while frontier:
        seen.extend(frontier)
        chunk, frontier = frontier, []
        for i in range(0, len(chunk), 500):
            rows = await s.execute(select(Run.id).where(Run.parent_run_id.in_(chunk[i : i + 500])))
            frontier.extend(r for r in rows.scalars() if r not in seen)
    return seen


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def remove_blob_dirs(root: Path, run_ids: Iterable[uuid.UUID], *, dry_run: bool = False) -> tuple[int, int]:
    """Delete ``root/<run_id>`` for each run. Returns (directories, bytes)."""
    count = size = 0
    resolved_root = root.resolve()
    for rid in run_ids:
        target = (root / str(rid)).resolve()
        if target.parent != resolved_root or not target.is_dir():
            continue
        count += 1
        size += _dir_size(target)
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
    return count, size


def _blob_dir_candidates(root: Path) -> list[uuid.UUID]:
    if not root.is_dir():
        return []
    now = time.time()
    candidates: list[uuid.UUID] = []
    for entry in root.iterdir():
        try:
            rid = uuid.UUID(entry.name)
            if not entry.is_dir() or now - entry.stat().st_mtime < _ORPHAN_GRACE_S:
                continue
        except (ValueError, OSError):
            continue
        candidates.append(rid)
    return candidates


async def _orphan_blob_dirs(s: AsyncSession, root: Path) -> list[uuid.UUID]:
    candidates = await asyncio.to_thread(_blob_dir_candidates, root)
    orphans: list[uuid.UUID] = []
    for i in range(0, len(candidates), 500):
        chunk = candidates[i : i + 500]
        existing = set((await s.execute(select(Run.id).where(Run.id.in_(chunk)))).scalars())
        orphans.extend(r for r in chunk if r not in existing)
    return orphans


async def _count(s: AsyncSession, stmt) -> int:  # type: ignore[no-untyped-def]
    return int((await s.execute(stmt)).scalar_one())


async def apply_retention(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    max_batches: int = 1000,
) -> RetentionStats:
    """Apply the configured policy. Each batch commits on its own so locks stay short."""
    policy = RetentionPolicy.from_settings(settings)
    now = now or datetime.now(UTC)
    started = time.perf_counter()
    stats = RetentionStats(dry_run=dry_run, started_at=now.isoformat())
    root = blob_root(settings)
    batch = policy.batch_size

    run_cut = _cutoff(policy.run_days, now)
    event_cut = _cutoff(policy.event_days, now)
    audit_cut = _cutoff(policy.audit_days, now)
    ledger_cut = _cutoff(policy.ledger_days, now)

    doomed: set[uuid.UUID] = set()
    async with sessionmaker() as s:
        # 1. Whole runs (top-level only; sub-runs follow their parent through the FK cascade).
        if run_cut is not None:
            eligible = select(Run.id).where(Run.parent_run_id.is_(None), _finished_before(run_cut))
            if dry_run:
                roots = list((await s.execute(eligible)).scalars())
                ids = await descendant_run_ids(s, roots)
                doomed.update(ids)
                stats.runs = len(ids)
                for i in range(0, len(ids), 500):
                    stats.run_events += await _count(
                        s, select(func.count(RunEvent.id)).where(RunEvent.run_id.in_(ids[i : i + 500]))
                    )
                d, b = remove_blob_dirs(root, ids, dry_run=True)
                stats.blob_dirs += d
                stats.blob_bytes += b
            else:
                for _ in range(max_batches):
                    roots = list((await s.execute(eligible.limit(batch))).scalars())
                    if not roots:
                        break
                    ids = await descendant_run_ids(s, roots)
                    for i in range(0, len(ids), 500):
                        stats.run_events += await _count(
                            s, select(func.count(RunEvent.id)).where(RunEvent.run_id.in_(ids[i : i + 500]))
                        )
                    await s.execute(delete(Run).where(Run.id.in_(roots)))
                    await s.commit()
                    stats.runs += len(ids)
                    d, b = await asyncio.to_thread(remove_blob_dirs, root, ids)
                    stats.blob_dirs += d
                    stats.blob_bytes += b
                    if len(roots) < batch:
                        break

        # 2. Event logs of finished runs (run row, result and artifacts are kept).
        if event_cut is not None:
            finished = select(Run.id).where(_finished_before(event_cut))
            if dry_run:
                # Runs that step 1 would delete are already counted there.
                ids = [r for r in (await s.execute(finished)).scalars() if r not in doomed]
                for i in range(0, len(ids), 500):
                    stats.run_events += await _count(
                        s, select(func.count(RunEvent.id)).where(RunEvent.run_id.in_(ids[i : i + 500]))
                    )
            else:
                for _ in range(max_batches):
                    ids = list(
                        (
                            await s.execute(
                                select(RunEvent.id).where(RunEvent.run_id.in_(finished)).limit(batch)
                            )
                        ).scalars()
                    )
                    if not ids:
                        break
                    await s.execute(delete(RunEvent).where(RunEvent.id.in_(ids)))
                    await s.commit()
                    stats.run_events += len(ids)
                    if len(ids) < batch:
                        break

        # 3. Audit trail.
        if audit_cut is not None:
            stats.audit_events = await _prune_by_id(s, AuditEvent, AuditEvent.ts < audit_cut, batch, dry_run)

        # 4. Ledger (opt-in only, see module docstring).
        if ledger_cut is not None:
            stats.ledger_entries = await _prune_by_id(
                s, LedgerEntry, LedgerEntry.ts < ledger_cut, batch, dry_run
            )

        # 5. Blob directories left behind by runs deleted elsewhere.
        orphans = await _orphan_blob_dirs(s, root)
        if orphans:
            d, b = await asyncio.to_thread(remove_blob_dirs, root, orphans, dry_run=dry_run)
            stats.blob_dirs += d
            stats.blob_bytes += b

    stats.finished_at = datetime.now(UTC).isoformat()
    stats.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info("retention.applied", **{k: v for k, v in stats.as_dict().items() if k != "error"})
    return stats


async def _prune_by_id(s: AsyncSession, model, condition, batch: int, dry_run: bool) -> int:  # type: ignore[no-untyped-def]
    if dry_run:
        return await _count(s, select(func.count(model.id)).where(condition))
    total = 0
    while True:
        ids = list((await s.execute(select(model.id).where(condition).limit(batch))).scalars())
        if not ids:
            return total
        await s.execute(delete(model).where(model.id.in_(ids)))
        await s.commit()
        total += len(ids)
        if len(ids) < batch:
            return total


class RetentionService:
    """Holds the last run's stats and drives the periodic loop."""

    def __init__(self, sessionmaker: async_sessionmaker, settings: Settings) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.last: RetentionStats | None = None
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> RetentionPolicy:
        return RetentionPolicy.from_settings(self.settings)

    async def run(self, *, dry_run: bool = False) -> RetentionStats:
        if dry_run:
            return await apply_retention(self.sessionmaker, self.settings, dry_run=True)
        async with self._lock:
            try:
                stats = await apply_retention(self.sessionmaker, self.settings)
            except Exception as exc:
                stats = RetentionStats(started_at=datetime.now(UTC).isoformat(), error=str(exc)[:300])
                log.error("retention.failed", error=str(exc)[:300])
                self.last = stats
                raise
            self.last = stats
            return stats

    async def loop(self) -> None:
        """Hourly (configurable) with jitter, so replicas do not prune in lock-step."""
        interval = self.policy.interval_s
        await asyncio.sleep(random.uniform(0.1, 0.5) * interval)  # noqa: S311 - jitter, not crypto
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: S110 - logged in run(); keep the loop alive
                pass
            await asyncio.sleep(interval * random.uniform(0.9, 1.1))  # noqa: S311
