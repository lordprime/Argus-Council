"""Data retention, account export and account deletion."""

from __future__ import annotations

import io
import os
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
from sqlalchemy import func, select

from argus.db.base import Base
from argus.db.models import (
    Artifact,
    AuditEvent,
    Council,
    CouncilMember,
    LedgerEntry,
    Run,
    RunEvent,
    Skill,
    User,
)
from tests.conftest import register_and_login

PW = "correct horse battery staple"
NOW = datetime.now(UTC)


async def _uid(client, headers) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])


async def _make_run(
    app,
    owner: uuid.UUID,
    *,
    age_days: float,
    status: str = "completed",
    parent: uuid.UUID | None = None,
    events: int = 3,
    blob: bytes | None = b"PDFDATA",
) -> uuid.UUID:
    finished = NOW - timedelta(days=age_days)
    async with app.state.db.sessionmaker() as s:
        run = Run(
            owner_id=owner,
            topic=f"topic {age_days}",
            status=status,
            result="# Final answer",
            parent_run_id=parent,
            finished_at=finished if status in ("completed", "failed", "cancelled", "halted") else None,
        )
        s.add(run)
        await s.flush()
        for i in range(1, events + 1):
            s.add(
                RunEvent(
                    run_id=run.id, seq=i, event_id=f"e{i}", type="message", ts=finished, payload={"i": i}
                )
            )
        s.add(
            Artifact(run_id=run.id, path="report.md", kind="file", size=5, sha256="0" * 64, content="hello")
        )
        if blob is not None:
            root = Path(app.state.settings.data_dir) / "artifacts" / str(run.id)
            root.mkdir(parents=True, exist_ok=True)
            (root / "abc").write_bytes(blob)
            s.add(
                Artifact(
                    run_id=run.id,
                    path="exports/report.pdf",
                    kind="export",
                    mime="application/pdf",
                    size=len(blob),
                    sha256="a" * 64,
                    blob_path=f"{run.id}/abc",
                )
            )
        await s.commit()
        return run.id


def _blob_dir(app, run_id: uuid.UUID) -> Path:
    return Path(app.state.settings.data_dir) / "artifacts" / str(run_id)


async def _count(app, stmt) -> int:
    async with app.state.db.sessionmaker() as s:
        return int((await s.execute(stmt)).scalar_one())


# ---------------------------------------------------------------- retention
async def test_run_retention_prunes_only_eligible_runs_and_blobs(app, client, auth):
    uid = await _uid(client, auth)
    old = await _make_run(app, uid, age_days=90)
    child = await _make_run(app, uid, age_days=90, parent=old)
    recent = await _make_run(app, uid, age_days=5)
    running = await _make_run(app, uid, age_days=90, status="running", blob=None)
    app.state.settings.retention_run_days = 30

    r = await client.post("/api/v1/admin/retention/run", json={"dry_run": True}, headers=auth)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["dry_run"] is True
    assert preview["runs"] == 2 and preview["run_events"] == 6 and preview["blob_dirs"] == 2
    assert await _count(app, select(func.count(Run.id))) == 4  # nothing deleted
    assert _blob_dir(app, old).is_dir()

    r = await client.post("/api/v1/admin/retention/run", json={}, headers=auth)
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["runs"] == 2 and stats["blob_dirs"] == 2 and stats["blob_bytes"] == 14
    remaining = {rid for rid in (await _all_run_ids(app))}
    assert remaining == {recent, running}
    assert not _blob_dir(app, old).exists() and not _blob_dir(app, child).exists()
    assert _blob_dir(app, recent).is_dir()
    assert await _count(app, select(func.count(RunEvent.id)).where(RunEvent.run_id == old)) == 0

    got = (await client.get("/api/v1/admin/retention", headers=auth)).json()
    assert got["policy"]["run_days"] == 30 and got["last_run"]["runs"] == 2


async def _all_run_ids(app) -> list[uuid.UUID]:
    async with app.state.db.sessionmaker() as s:
        return list((await s.execute(select(Run.id))).scalars())


async def test_event_retention_keeps_run_result_and_artifacts(app, client, auth):
    uid = await _uid(client, auth)
    old = await _make_run(app, uid, age_days=40)
    recent = await _make_run(app, uid, age_days=1)
    live = await _make_run(app, uid, age_days=40, status="paused")
    app.state.settings.retention_event_days = 10

    preview = (await client.post("/api/v1/admin/retention/run", json={"dry_run": True}, headers=auth)).json()
    assert preview["run_events"] == 3 and preview["runs"] == 0

    stats = (await client.post("/api/v1/admin/retention/run", headers=auth)).json()
    assert stats["run_events"] == 3 and stats["runs"] == 0
    events = select(func.count(RunEvent.id))
    assert await _count(app, events.where(RunEvent.run_id == old)) == 0
    assert await _count(app, events.where(RunEvent.run_id == recent)) == 3
    assert await _count(app, events.where(RunEvent.run_id == live)) == 3
    r = await client.get(f"/api/v1/runs/{old}", headers=auth)
    assert r.status_code == 200 and r.json()["result"] == "# Final answer"
    assert await _count(app, select(func.count(Artifact.id)).where(Artifact.run_id == old)) == 2
    assert _blob_dir(app, old).is_dir()


async def test_audit_pruned_ledger_kept_by_default(app, client, auth):
    uid = await _uid(client, auth)
    async with app.state.db.sessionmaker() as s:
        s.add(AuditEvent(ts=NOW - timedelta(days=400), action="old", detail={}))
        s.add(AuditEvent(ts=NOW - timedelta(days=10), action="new", detail={}))
        s.add(
            LedgerEntry(
                owner_id=uid,
                seq=1,
                ts=NOW - timedelta(days=4000),
                protocol="mcp",
                direction="outbound",
                peer="x",
                method="m",
                status="ok",
                request_sha256="0" * 64,
                response_sha256="0" * 64,
                prev_hash="0" * 64,
                hash="1" * 64,
            )
        )
        await s.commit()
    policy = (await client.get("/api/v1/admin/retention", headers=auth)).json()["policy"]
    assert policy["audit_days"] == 365 and policy["ledger_days"] == 0 and policy["run_days"] == 0
    assert any("ledger" in n.lower() for n in policy["notes"])

    stats = (await client.post("/api/v1/admin/retention/run", json={"dry_run": False}, headers=auth)).json()
    assert stats["audit_events"] == 1 and stats["ledger_entries"] == 0
    assert await _count(app, select(func.count(AuditEvent.id)).where(AuditEvent.action == "old")) == 0
    assert await _count(app, select(func.count(AuditEvent.id)).where(AuditEvent.action == "new")) == 1
    assert await _count(app, select(func.count(LedgerEntry.id))) == 1


async def test_orphan_blob_dirs_are_swept(app, client, auth):
    orphan = _blob_dir(app, uuid.uuid4())
    orphan.mkdir(parents=True)
    (orphan / "blob").write_bytes(b"x" * 10)
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    fresh = _blob_dir(app, uuid.uuid4())
    fresh.mkdir(parents=True)
    stats = (await client.post("/api/v1/admin/retention/run", headers=auth)).json()
    assert stats["blob_dirs"] == 1 and stats["blob_bytes"] == 10
    assert not orphan.exists() and fresh.exists()


async def test_webhook_deliveries_and_notifications_are_pruned(app, client, auth):
    from argus.db.models import Notification, Webhook, WebhookDelivery

    uid = await _uid(client, auth)
    async with app.state.db.sessionmaker() as s:
        hook = Webhook(owner_id=uid, url="https://h.example.com", events=["*"], secret_enc="x")
        s.add(hook)
        await s.flush()
        for age, name in ((40, "old"), (5, "new")):
            s.add(
                WebhookDelivery(
                    webhook_id=hook.id, ts=NOW - timedelta(days=age), event=name, delivery_id=name
                )
            )
        s.add(Notification(user_id=uid, kind="k", title="old", created_at=NOW - timedelta(days=120)))
        s.add(Notification(user_id=uid, kind="k", title="new", created_at=NOW - timedelta(days=1)))
        await s.commit()
    policy = (await client.get("/api/v1/admin/retention", headers=auth)).json()["policy"]
    assert policy["webhook_delivery_days"] == 30 and policy["notification_days"] == 90

    dry = (await client.post("/api/v1/admin/retention/run", json={"dry_run": True}, headers=auth)).json()
    assert dry["webhook_deliveries"] == 1 and dry["notifications"] == 1
    stats = (await client.post("/api/v1/admin/retention/run", json={"dry_run": False}, headers=auth)).json()
    assert stats["webhook_deliveries"] == 1 and stats["notifications"] == 1
    async with app.state.db.sessionmaker() as s:
        assert list((await s.execute(select(WebhookDelivery.event))).scalars()) == ["new"]
        assert list((await s.execute(select(Notification.title))).scalars()) == ["new"]


async def test_retention_endpoints_are_admin_only(client, auth):
    member = await register_and_login(client, "member@example.com")
    assert (await client.get("/api/v1/admin/retention", headers=member)).status_code == 403
    assert (await client.post("/api/v1/admin/retention/run", headers=member)).status_code == 403


# ---------------------------------------------------------------- export
async def test_export_zip_contains_only_own_data(app, client, auth):
    uid = await _uid(client, auth)
    other = await register_and_login(client, "other@example.com")
    other_uid = await _uid(client, other)
    mine = await _make_run(app, uid, age_days=1)
    theirs = await _make_run(app, other_uid, age_days=1)
    async with app.state.db.sessionmaker() as s:
        council = Council(owner_id=uid, name="Board")
        s.add(council)
        await s.flush()
        s.add(CouncilMember(council_id=council.id, name="Ada", role="chair"))
        s.add(Skill(owner_id=uid, name="summarise", description="d", body="b"))
        s.add(Skill(owner_id=other_uid, name="secret-skill", description="d", body="b"))
        await s.commit()
    await client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=auth)
    tables = Base.metadata.tables
    has_kb = {"knowledge_bases", "knowledge_documents", "knowledge_chunks"} <= set(tables)
    if has_kb:  # feature tables are picked up generically, including rows owned via a parent
        async with app.state.db.sessionmaker() as s:
            kb, doc = uuid.uuid4(), uuid.uuid4()
            await s.execute(tables["knowledge_bases"].insert().values(id=kb, owner_id=uid, name="kb"))
            await s.execute(
                tables["knowledge_documents"]
                .insert()
                .values(id=doc, kb_id=kb, filename="notes.md", content_type="text/markdown")
            )
            await s.execute(
                tables["knowledge_chunks"].insert().values(kb_id=kb, document_id=doc, text="chunk text")
            )
            await s.commit()

    r = await client.post("/api/v1/me/export", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"manifest.json", "account.json", "councils.json", "skills.json", "runs/index.json"} <= names
    assert f"runs/{mine}/run.json" in names and f"runs/{mine}/events.jsonl" in names
    assert f"runs/{mine}/result.md" in names
    assert zf.read(f"runs/{mine}/artifacts/v1/report.md") == b"hello"
    assert zf.read(f"runs/{mine}/artifacts/v1/exports/report.pdf") == b"PDFDATA"
    assert not any(str(theirs) in n for n in names)
    account = orjson.loads(zf.read("account.json"))
    assert account["email"] == "admin@example.com" and "password_hash" not in account
    councils = orjson.loads(zf.read("councils.json"))
    assert councils[0]["name"] == "Board" and councils[0]["members"][0]["name"] == "Ada"
    assert [s["name"] for s in orjson.loads(zf.read("skills.json"))] == ["summarise"]
    keys = orjson.loads(zf.read("api_keys.json"))
    assert keys and "key_hash" not in keys[0]
    manifest = orjson.loads(zf.read("manifest.json"))
    assert manifest["counts"]["runs"] == 1 and manifest["counts"]["run_events"] == 3
    if has_kb:
        assert orjson.loads(zf.read("knowledge_chunks.json"))[0]["text"] == "chunk text"
        assert manifest["counts"]["knowledge_documents"] == 1
    assert b"argon2" not in b"".join(zf.read(n) for n in names)


async def test_export_requires_session_and_respects_cap(app, client, auth):
    assert (await client.post("/api/v1/me/export")).status_code == 401
    key = (await client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=auth)).json()["key"]
    assert (await client.post("/api/v1/me/export", headers={"X-API-Key": key})).status_code == 403
    uid = await _uid(client, auth)
    await _make_run(app, uid, age_days=1, blob=os.urandom(4096))
    app.state.settings.export_max_bytes = 2048
    r = await client.post("/api/v1/me/export", headers=auth)
    assert r.status_code == 413 and r.json()["error"]["code"] == "export_too_large"


# ---------------------------------------------------------------- deletion
async def test_delete_account_removes_data_and_blobs(app, client, auth):
    member = await register_and_login(client, "leaver@example.com")
    uid = await _uid(client, member)
    admin_uid = await _uid(client, auth)
    run = await _make_run(app, uid, age_days=1)
    kept = await _make_run(app, admin_uid, age_days=1)

    r = await client.request("DELETE", "/api/v1/me", json={"password": "wrong password!!"}, headers=member)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_password"
    assert (await client.request("DELETE", "/api/v1/me", headers=member)).status_code == 422

    r = await client.request("DELETE", "/api/v1/me", json={"password": PW}, headers=member)
    assert r.status_code == 204, r.text
    async with app.state.db.sessionmaker() as s:
        assert await s.get(User, uid) is None
        assert await s.get(Run, run) is None and await s.get(Run, kept) is not None
    assert not _blob_dir(app, run).exists() and _blob_dir(app, kept).is_dir()
    login = await client.post("/api/v1/auth/login", json={"email": "leaver@example.com", "password": PW})
    assert login.status_code == 401
    assert (await client.get("/api/v1/auth/me", headers=member)).status_code == 401

    # Audited without referencing the deleted user row.
    await _drain(app)
    async with app.state.db.sessionmaker() as s:
        row = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "DELETE /me", AuditEvent.success))
        ).scalar_one()
    assert row.actor_id is None and row.detail["deleted_user_id"] == str(uid)


async def _drain(app) -> None:
    import asyncio

    while pending := [t for t in app.state.background if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_last_admin_cannot_delete_account(client, auth):
    r = await client.request("DELETE", "/api/v1/me", json={"password": PW}, headers=auth)
    assert r.status_code == 409
    second = await register_and_login(client, "second@example.com")
    sid = await _uid(client, second)
    r = await client.patch(f"/api/v1/admin/users/{sid}", json={"role": "admin"}, headers=auth)
    assert r.status_code == 200, r.text
    assert (
        await client.request("DELETE", "/api/v1/me", json={"password": PW}, headers=auth)
    ).status_code == 204
    users = (await client.get("/api/v1/admin/users", headers=second)).json()
    assert [u["email"] for u in users] == ["second@example.com"]
