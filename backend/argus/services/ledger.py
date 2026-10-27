"""Hash-chained ledger of every MCP / A2A / ACP interaction.

Each entry stores digests (not raw payloads) of the request and response plus
metadata, and ``hash = sha256(prev_hash || canonical(entry))``. Editing or
deleting any row breaks the chain, which ``verify`` detects.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

import orjson
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.metrics import PROTOCOL_CALLS
from argus.db.models import LedgerEntry

GENESIS = "0" * 64
# Serialises appends per owner within a process; the (owner_id, seq) unique constraint plus a
# retry covers concurrent writers in other processes.
_locks: dict[tuple[int, uuid.UUID], asyncio.Lock] = defaultdict(asyncio.Lock)


Purpose = Literal["run", "discover", "refresh"]
_TAGGED: tuple[str, ...] = ("run", "discover", "refresh")


def purpose_is(purpose: Purpose) -> ColumnElement[bool]:
    """SQL condition for an entry's purpose: ``detail.purpose`` when it is one of the known values,
    otherwise "run" for entries tied to a run. Mirrors ``entryPurpose`` in the frontend."""
    tag = LedgerEntry.detail["purpose"].as_string()
    if purpose != "run":
        return tag == purpose
    untagged = func.coalesce(tag, "").not_in(_TAGGED)
    return or_(tag == "run", and_(untagged, LedgerEntry.run_id.is_not(None)))


def digest(obj: Any) -> str:
    if isinstance(obj, bytes):
        raw = obj
    elif isinstance(obj, str):
        raw = obj.encode()
    else:
        raw = orjson.dumps(obj, option=orjson.OPT_SORT_KEYS, default=str)
    return hashlib.sha256(raw).hexdigest()


def _canonical(e: LedgerEntry) -> bytes:
    body = {
        "seq": e.seq,
        "ts": e.ts.astimezone(UTC).isoformat() if e.ts.tzinfo else e.ts.replace(tzinfo=UTC).isoformat(),
        "protocol": e.protocol,
        "direction": e.direction,
        "peer": e.peer,
        "method": e.method,
        "status": e.status,
        "request_sha256": e.request_sha256,
        "response_sha256": e.response_sha256,
        "latency_ms": e.latency_ms,
        "run_id": str(e.run_id) if e.run_id else None,
        "detail": e.detail,
    }
    return orjson.dumps(body, option=orjson.OPT_SORT_KEYS)


def chain_hash(prev: str, e: LedgerEntry) -> str:
    return hashlib.sha256(prev.encode() + _canonical(e)).hexdigest()


async def record(
    sessionmaker: async_sessionmaker,
    *,
    owner_id: uuid.UUID,
    protocol: str,
    direction: str,
    peer: str,
    method: str,
    status: str,
    request: Any,
    response: Any,
    latency_ms: int = 0,
    run_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> LedgerEntry:
    PROTOCOL_CALLS.labels(protocol, direction, "ok" if status in ("ok", "completed") else "error").inc()
    lock = _locks[(id(asyncio.get_running_loop()), owner_id)]
    for attempt in range(3):
        try:
            async with lock:
                return await _append(
                    sessionmaker,
                    owner_id,
                    protocol,
                    direction,
                    peer,
                    method,
                    status,
                    request,
                    response,
                    latency_ms,
                    run_id,
                    detail,
                )
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def _append(
    sessionmaker,
    owner_id,
    protocol,
    direction,
    peer,
    method,
    status,
    request,
    response,
    latency_ms,
    run_id,
    detail,
) -> LedgerEntry:
    async with sessionmaker() as s:
        last = (
            await s.execute(
                select(LedgerEntry)
                .where(LedgerEntry.owner_id == owner_id)
                .order_by(LedgerEntry.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        entry = LedgerEntry(
            owner_id=owner_id,
            seq=(last.seq + 1) if last else 1,
            ts=datetime.now(UTC).replace(microsecond=0),
            protocol=protocol,
            direction=direction,
            peer=peer[:500],
            method=method[:120],
            status=status[:16],
            request_sha256=digest(request),
            response_sha256=digest(response),
            latency_ms=latency_ms,
            run_id=run_id,
            detail=detail or {},
            prev_hash=last.hash if last else GENESIS,
        )
        entry.hash = chain_hash(entry.prev_hash, entry)
        s.add(entry)
        await s.commit()
        return entry


async def verify(sessionmaker: async_sessionmaker, owner_id: uuid.UUID) -> dict[str, Any]:
    async with sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    select(LedgerEntry).where(LedgerEntry.owner_id == owner_id).order_by(LedgerEntry.seq)
                )
            ).scalars()
        )
    prev = GENESIS
    for i, e in enumerate(rows, start=1):
        if e.seq != i:
            return {"ok": False, "entries": len(rows), "broken_at": e.seq, "reason": "sequence gap"}
        if e.prev_hash != prev or chain_hash(prev, e) != e.hash:
            return {"ok": False, "entries": len(rows), "broken_at": e.seq, "reason": "hash mismatch"}
        prev = e.hash
    return {"ok": True, "entries": len(rows), "head": prev}
