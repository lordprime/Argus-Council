"""Account export and erasure cover everything a user owns, in the database and on disk."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import orjson
from sqlalchemy import func, select

from argus.db.base import Base
from argus.db.models import (
    Artifact,
    AuditEvent,
    CliCredential,
    CliSession,
    Council,
    CouncilMember,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    LedgerEntry,
    McpServer,
    MemoryItem,
    Notification,
    ProviderConfig,
    RemoteAgent,
    Run,
    RunEvent,
    RunTemplate,
    Skill,
    SkillVersion,
    Webhook,
    WebhookDelivery,
    WebhookOutbox,
)
from tests.conftest import register_and_login

PW = "correct horse battery staple"
NOW = datetime.now(UTC)

# Tables exported with their own layout rather than as <table>.json.
SPECIAL_LAYOUT = {"users", "runs", "run_events", "artifacts", "council_members"}


async def _uid(client, headers) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me", headers=headers)).json()["id"])


def _blob_root(app) -> Path:
    return Path(app.state.settings.data_dir) / "artifacts"


async def _seed_everything(app, uid: uuid.UUID, email: str) -> dict[str, uuid.UUID]:
    """One row in every user-owned table, plus files on disk."""
    async with app.state.db.sessionmaker() as s:
        council = Council(owner_id=uid, name="Board")
        s.add(council)
        await s.flush()
        s.add(CouncilMember(council_id=council.id, name="Ada", role="chair"))
        s.add(ProviderConfig(owner_id=uid, name="p", kind="openai", api_key_enc="ENC-SECRET"))
        s.add(CliCredential(owner_id=uid, cli="claude", api_key_enc="ENC-SECRET", key_hint="sk-…abcd"))
        run = Run(owner_id=uid, topic="t", status="completed", result="done")
        s.add(run)
        await s.flush()
        s.add(CliSession(owner_id=uid, cli="claude", session_id=str(uuid.uuid4()), run_id=run.id, label=""))
        sub = Run(owner_id=uid, topic="sub", status="completed", parent_run_id=run.id)
        s.add(sub)
        await s.flush()
        s.add(RunEvent(run_id=run.id, seq=1, event_id="e1", type="run.status", ts=NOW, payload={}))
        root = _blob_root(app)
        for rid in (run.id, sub.id):
            (root / str(rid)).mkdir(parents=True, exist_ok=True)
        (root / str(run.id) / "referenced").write_bytes(b"REFERENCED")
        # A blob on disk that no artifact row points at (for example a write whose row was lost).
        (root / str(run.id) / "stray").write_bytes(b"STRAY")
        (root / str(sub.id) / "subblob").write_bytes(b"SUB")
        s.add(
            Artifact(
                run_id=run.id,
                path="out/report.pdf",
                kind="export",
                size=10,
                sha256="a" * 64,
                blob_path=f"{run.id}/referenced",
            )
        )
        s.add(
            Artifact(
                run_id=sub.id,
                path="x.pdf",
                kind="export",
                size=3,
                sha256="b" * 64,
                blob_path=f"{sub.id}/subblob",
            )
        )
        skill = Skill(owner_id=uid, name="summarise", description="d", body="b")
        s.add(skill)
        await s.flush()
        s.add(SkillVersion(skill_id=skill.id, number=1, skill_md="md", author_id=uid, author=email))
        s.add(McpServer(owner_id=uid, name="gh", url="https://mcp.example.com", headers_enc="ENC-SECRET"))
        s.add(RemoteAgent(owner_id=uid, name="ra", protocol="a2a", url="https://a2a.example.com"))
        s.add(
            LedgerEntry(
                owner_id=uid,
                seq=1,
                ts=NOW,
                protocol="mcp",
                direction="outbound",
                peer="gh",
                method="tools/list",
                status="ok",
                request_sha256="0" * 64,
                response_sha256="0" * 64,
                prev_hash="0" * 64,
                hash="1" * 64,
            )
        )
        hook = Webhook(owner_id=uid, url="https://hooks.example.com", events=["*"], secret_enc="ENC-SECRET")
        s.add(hook)
        await s.flush()
        s.add(WebhookDelivery(webhook_id=hook.id, ts=NOW, event="run.completed", delivery_id="d1", ok=True))
        s.add(
            WebhookOutbox(
                webhook_id=hook.id,
                event="run.completed",
                delivery_id="d2",
                body="{}",
                attempts=0,
                next_attempt_at=NOW.replace(year=2100),  # never due during the test
                created_at=NOW,
            )
        )
        s.add(RunTemplate(owner_id=uid, name="tpl", topic="t"))
        s.add(Notification(user_id=uid, kind="run.completed", title="done", run_id=run.id))
        kb = KnowledgeBase(owner_id=uid, name="kb")
        s.add(kb)
        await s.flush()
        doc = KnowledgeDocument(kb_id=kb.id, filename="n.md", content_type="text/markdown")
        s.add(doc)
        await s.flush()
        s.add(KnowledgeChunk(kb_id=kb.id, document_id=doc.id, text="chunk"))
        s.add(MemoryItem(owner_id=uid, council_id=council.id, text="remember this", run_id=run.id))
        s.add(
            AuditEvent(
                ts=NOW,
                actor_id=uid,
                action="POST /runs",
                ip="203.0.113.7",
                user_agent="Browser/1",
                detail={"email": email},
            )
        )
        # A failed login against this account carries no actor, only the email.
        s.add(
            AuditEvent(
                ts=NOW, actor_id=None, action="POST /auth/login", success=False, detail={"email": email}
            )
        )
        await s.commit()
        return {"run": run.id, "sub": sub.id, "hook": hook.id}


async def test_export_covers_every_table_and_files_on_disk(app, client, auth):
    uid = await _uid(client, auth)
    ids = await _seed_everything(app, uid, "admin@example.com")
    await client.post("/api/v1/auth/api-keys", json={"name": "k"}, headers=auth)
    r = await client.post("/api/v1/me/export", headers=auth)
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())

    # Every table in the schema is either exported as <table>.json or has its own layout.
    for table in Base.metadata.tables:
        if table in SPECIAL_LAYOUT:
            continue
        assert f"{table}.json" in names, f"{table} missing from the export"
        assert orjson.loads(zf.read(f"{table}.json")), f"{table}.json is empty"
    manifest = orjson.loads(zf.read("manifest.json"))
    assert manifest["skipped_tables"] == []

    run, sub = ids["run"], ids["sub"]
    assert zf.read(f"runs/{run}/artifacts/v1/out/report.pdf") == b"REFERENCED"
    assert zf.read(f"runs/{sub}/artifacts/v1/x.pdf") == b"SUB"
    # Files on disk that no artifact row references are exported too.
    assert zf.read(f"runs/{run}/files/stray") == b"STRAY"
    assert f"runs/{run}/files/referenced" not in names  # already exported as an artifact
    assert manifest["counts"]["run_files"] == 1
    index = {e["id"]: e for e in orjson.loads(zf.read("runs/index.json"))}
    assert index[str(sub)]["parent_run_id"] == str(run)

    deliveries = orjson.loads(zf.read("webhook_deliveries.json"))
    assert deliveries[0]["delivery_id"] == "d1"
    everything = b"".join(zf.read(n) for n in names)
    assert b"ENC-SECRET" not in everything and b"argon2" not in everything


async def test_delete_account_leaves_nothing_behind(app, client, auth):
    member = await register_and_login(client, "leaver@example.com")
    uid = await _uid(client, member)
    ids = await _seed_everything(app, uid, "leaver@example.com")
    keep = await _seed_everything(app, await _uid(client, auth), "admin@example.com")

    r = await client.request("DELETE", "/api/v1/me", json={"password": PW}, headers=member)
    assert r.status_code == 204, r.text

    # No row anywhere still points at the user, directly or through a deleted parent.
    async with app.state.db.sessionmaker() as s:
        for table in Base.metadata.sorted_tables:
            for col in ("owner_id", "user_id", "actor_id", "author_id"):
                if col in table.c:
                    n = (
                        await s.execute(select(func.count()).select_from(table).where(table.c[col] == uid))
                    ).scalar()
                    assert n == 0, f"{table.name}.{col} still references the deleted user"
        for model, col, parent in (
            (CouncilMember, CouncilMember.council_id, Council),
            (RunEvent, RunEvent.run_id, Run),
            (Artifact, Artifact.run_id, Run),
            (SkillVersion, SkillVersion.skill_id, Skill),
            (WebhookDelivery, WebhookDelivery.webhook_id, Webhook),
            (WebhookOutbox, WebhookOutbox.webhook_id, Webhook),
            (KnowledgeDocument, KnowledgeDocument.kb_id, KnowledgeBase),
            (KnowledgeChunk, KnowledgeChunk.kb_id, KnowledgeBase),
        ):
            orphans = (
                await s.execute(select(func.count()).select_from(model).where(col.not_in(select(parent.id))))
            ).scalar()
            assert orphans == 0, f"orphaned {model.__tablename__}"
        assert await s.get(Run, ids["sub"]) is None
        assert await s.get(Run, keep["sub"]) is not None
        # The audit trail keeps what happened, but not who it was: no email, IP or user agent.
        rows = list((await s.execute(select(AuditEvent))).scalars())
        for row in rows:
            assert row.detail.get("email") != "leaver@example.com", row.action
        assert not any(
            row.ip == "203.0.113.7" or row.user_agent == "Browser/1" for row in rows if row.actor_id is None
        )
        kept_admin = [r for r in rows if r.detail.get("email") == "admin@example.com"]
        assert len(kept_admin) >= 2  # the other account's audit trail is untouched
        versions = list((await s.execute(select(SkillVersion.author))).scalars())
        assert "leaver@example.com" not in versions

    # Files on disk: the user's run directories are gone, other users' are kept.
    root = _blob_root(app)
    assert not (root / str(ids["run"])).exists() and not (root / str(ids["sub"])).exists()
    assert (root / str(keep["run"]) / "stray").is_file()
