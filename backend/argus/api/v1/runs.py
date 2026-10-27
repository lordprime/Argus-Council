from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import orjson
from fastapi import APIRouter, Query, Request, status
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import load_only

from argus.api.deps import CacheDep, CurrentPrincipal, DbDep, SettingsDep, resolve_principal, write_scope
from argus.api.idempotency import idempotent
from argus.api.schemas import ApprovalDecisionIn, ExportIn, InterventionIn, RunDetail, RunIn, RunOut
from argus.core.errors import ArgusError, NotFound, Unauthorized
from argus.db.models import Artifact, Run
from argus.runtime.events import TERMINAL_STATUSES, load_events
from argus.runtime.manager import RunManager
from argus.runtime.snapshot import reduce_events
from argus.services.runs import create_and_start_run

# stream-ticket only grants read access to a run's own event stream.
router = APIRouter(
    prefix="/runs", tags=["runs"], dependencies=[write_scope("runs:write", exempt=("/stream-ticket",))]
)

TICKET_TTL_S = 300
HEARTBEAT_S = 15.0


def _manager(request: Request) -> RunManager:
    return request.app.state.runs


async def _own_run(db, owner_id: uuid.UUID, run_id: uuid.UUID) -> Run:
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != owner_id:
        raise NotFound("Run not found")
    return run


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def create_run(data: RunIn, principal: CurrentPrincipal, db: DbDep, request: Request, cache: CacheDep):
    principal.require_scope("runs:write")

    async def run() -> RunOut:
        return RunOut.model_validate(await create_and_start_run(db, _manager(request), principal.id, data))

    return await idempotent(request, cache, str(principal.id), 201, run)


@router.get("", response_model=list[RunOut])
async def list_runs(
    principal: CurrentPrincipal,
    db: DbDep,
    status_: str | None = Query(default=None, alias="status"),
    mode: str | None = None,
    council_id: uuid.UUID | None = None,
    before: datetime | None = Query(default=None, description="Cursor: return runs created before this time"),
    before_id: uuid.UUID | None = Query(
        default=None, description="Cursor tie-breaker: with `before`, the id of the last run already seen"
    ),
    limit: int = Query(default=50, ge=1, le=200),
):
    # Only the RunOut columns: never the result text or the council snapshot (polled every 10 s).
    q = (
        select(Run)
        .options(load_only(*(getattr(Run, f) for f in RunOut.model_fields)))
        .where(Run.owner_id == principal.id, Run.parent_run_id.is_(None))
    )
    if before and before_id:
        # Keyset on (created_at, id) so runs sharing a timestamp are not skipped at a page boundary.
        q = q.where(or_(Run.created_at < before, and_(Run.created_at == before, Run.id < before_id)))
    elif before:
        q = q.where(Run.created_at < before)
    if status_:
        q = q.where(Run.status == status_)
    if mode:
        q = q.where(Run.mode == mode)
    if council_id:
        q = q.where(Run.council_id == council_id)
    rows = await db.execute(q.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit))
    return list(rows.scalars())


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    return await _own_run(db, principal.id, run_id)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    run = await _own_run(db, principal.id, run_id)
    if run.status not in TERMINAL_STATUSES:
        raise ArgusError("Cancel the run before deleting it")
    await db.delete(run)


@router.post("/{run_id}/pause", status_code=status.HTTP_202_ACCEPTED)
async def pause(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    await _own_run(db, principal.id, run_id)
    await _manager(request).pause(run_id)
    return {"status": "paused"}


@router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    await _own_run(db, principal.id, run_id)
    await _manager(request).resume(run_id)
    return {"status": "running"}


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, request: Request) -> dict:
    await _own_run(db, principal.id, run_id)
    await _manager(request).cancel(run_id)
    return {"status": "cancelling"}


@router.post("/{run_id}/interventions", status_code=status.HTTP_202_ACCEPTED)
async def intervene(
    run_id: uuid.UUID, data: InterventionIn, principal: CurrentPrincipal, db: DbDep, request: Request
) -> dict:
    await _own_run(db, principal.id, run_id)
    name = principal.user.display_name or "You"
    return await _manager(request).intervene(run_id, data.model_dump(), user_name=name)


@router.post("/{run_id}/approvals/{approval_id}", status_code=status.HTTP_202_ACCEPTED)
async def decide(
    run_id: uuid.UUID,
    approval_id: str,
    data: ApprovalDecisionIn,
    principal: CurrentPrincipal,
    db: DbDep,
    request: Request,
) -> dict:
    await _own_run(db, principal.id, run_id)
    decision = {
        "decision": "approved" if data.decision == "approve" else "rejected",
        "by_user_id": str(principal.id),
        **({"edited_args": data.edited_args} if data.edited_args else {}),
        **({"reason": data.reason} if data.reason else {}),
    }
    await _manager(request).resolve_approval(run_id, approval_id, decision)
    return {"decision": decision["decision"]}


@router.get("/{run_id}/events")
async def events(
    run_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DbDep,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    await _own_run(db, principal.id, run_id)
    evs = await load_events(request.app.state.db.sessionmaker, run_id, after_seq, before_seq, limit)
    return {"events": evs, "has_more": len(evs) == limit}


@router.get("/{run_id}/snapshot")
async def snapshot(
    run_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DbDep,
    request: Request,
    at_seq: int | None = Query(default=None, ge=0),
) -> dict:
    await _own_run(db, principal.id, run_id)
    evs = await load_events(request.app.state.db.sessionmaker, run_id, 0, None, 100_000)
    return reduce_events(evs, at_seq)


@router.post("/{run_id}/stream-ticket")
async def stream_ticket(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, cache: CacheDep) -> dict:
    await _own_run(db, principal.id, run_id)
    ticket = secrets.token_urlsafe(32)
    await cache.set(
        cache.key("ticket", ticket), {"user": str(principal.id), "run": str(run_id)}, ttl=TICKET_TTL_S
    )
    return {"ticket": ticket, "expires_at": (datetime.now(UTC) + timedelta(seconds=TICKET_TTL_S)).isoformat()}


async def _sse(request: Request, run_id: uuid.UUID, after_seq: int) -> AsyncIterator[bytes]:
    broker = request.app.state.broker
    sessionmaker = request.app.state.db.sessionmaker
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    ready = asyncio.Event()

    async def pump() -> None:
        async for payload in broker.subscribe(str(run_id), ready):
            await queue.put(payload)

    pump_task = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(ready.wait(), 10)
        last = after_seq
        terminal = False
        # Replay durable history first, then switch to live.
        while True:
            batch = await load_events(sessionmaker, run_id, last, None, 1000)
            for ev in batch:
                last = ev["seq"]
                terminal = terminal or (
                    ev["type"] == "run.status" and ev["data"].get("status") in TERMINAL_STATUSES
                )
                yield _frame(ev["seq"], ev["type"], orjson.dumps(ev))
            if len(batch) < 1000:
                break
        if terminal:
            return
        while not await request.is_disconnected():
            try:
                payload = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            ev = orjson.loads(payload)
            if not ev.get("ephemeral"):
                if ev["seq"] <= last:
                    continue
                last = ev["seq"]
            yield _frame(ev["seq"], ev["type"], payload)
            if ev["type"] == "run.status" and ev["data"].get("status") in TERMINAL_STATUSES:
                return
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task


def _frame(seq: int, type_: str, data: bytes) -> bytes:
    return b"id: %d\nevent: %s\ndata: %s\n\n" % (seq, type_.encode(), data)


@router.get("/{run_id}/events/stream")
async def stream(
    run_id: uuid.UUID,
    request: Request,
    db: DbDep,
    cache: CacheDep,
    settings: SettingsDep,
    after_seq: int = Query(default=0, ge=0),
    ticket: str | None = None,
):
    if ticket:
        key = cache.key("ticket", ticket)
        claim = await cache.get(key)
        await cache.delete(key)  # single use
        if not claim or claim.get("run") != str(run_id):
            raise Unauthorized("Invalid or expired stream ticket")
        owner = uuid.UUID(claim["user"])
    else:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise Unauthorized("Authentication required")
        owner = (await resolve_principal(db, settings, auth[7:].strip())).id
    await _own_run(db, owner, run_id)
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        after_seq = max(after_seq, int(last_event_id))
    await db.close()  # don't hold a pooled connection for the life of the stream
    return StreamingResponse(
        _sse(request, run_id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{run_id}/artifacts")
async def artifacts(run_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> dict:
    await _own_run(db, principal.id, run_id)
    rows = await db.execute(
        select(
            Artifact.path,
            Artifact.version,
            Artifact.size,
            Artifact.kind,
            Artifact.mime,
            Artifact.created_by,
            Artifact.created_at,
        )
        .where(Artifact.run_id == run_id, Artifact.deleted.is_(False))
        .order_by(Artifact.path, Artifact.version)
    )
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r.path] = {
            "path": r.path,
            "version": r.version,
            "size": r.size,
            "kind": r.kind,
            "mime": r.mime,
            "created_by": r.created_by,
            "updated_at": r.created_at.isoformat(),
        }
    return {"artifacts": list(latest.values())}


@router.get("/{run_id}/artifacts/content", response_class=PlainTextResponse)
async def artifact_content(
    run_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DbDep,
    path: str = Query(max_length=500),
    version: int | None = Query(default=None, ge=1),
):
    await _own_run(db, principal.id, run_id)
    q = select(Artifact).where(Artifact.run_id == run_id, Artifact.path == path, Artifact.deleted.is_(False))
    q = q.where(Artifact.version == version) if version else q.order_by(Artifact.version.desc()).limit(1)
    art = (await db.execute(q)).scalar_one_or_none()
    if art is None:
        raise NotFound("Artifact not found")
    if art.content is None:
        raise ArgusError("Binary artifact: use /artifacts/download")
    # Served as plain text with nosniff so browsers never render model-produced HTML/JS inline.
    return PlainTextResponse(
        art.content or "", headers={"Content-Disposition": "inline", "X-Artifact-Version": str(art.version)}
    )


@router.post("/{run_id}/exports", status_code=status.HTTP_201_CREATED)
async def create_export(
    run_id: uuid.UUID, data: ExportIn, principal: CurrentPrincipal, db: DbDep, request: Request
) -> dict:
    await _own_run(db, principal.id, run_id)
    return await _manager(request).export(run_id, data.format)


@router.get("/{run_id}/artifacts/download")
async def download_artifact(
    run_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DbDep,
    request: Request,
    path: str = Query(max_length=500),
    version: int | None = Query(default=None, ge=1),
):
    await _own_run(db, principal.id, run_id)
    q = select(Artifact).where(Artifact.run_id == run_id, Artifact.path == path, Artifact.deleted.is_(False))
    q = q.where(Artifact.version == version) if version else q.order_by(Artifact.version.desc()).limit(1)
    art = (await db.execute(q)).scalar_one_or_none()
    if art is None:
        raise NotFound("Artifact not found")
    if art.content is not None:
        data = art.content.encode()
    else:
        root = _manager(request).blob_root.resolve()
        blob = (root / (art.blob_path or "")).resolve()
        if root not in blob.parents or not blob.is_file():
            raise NotFound("Artifact content missing")
        data = blob.read_bytes()
    filename = art.path.rsplit("/", 1)[-1].replace('"', "")
    return Response(
        data,
        media_type=art.mime,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Artifact-Version": str(art.version),
            "X-Content-SHA256": art.sha256,
        },
    )
