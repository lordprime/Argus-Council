"""Knowledge-base ingestion and BM25 search, plus council-scoped long-term memory.

Every owner also has one *research library* (``KnowledgeBase.kind == "library"``): every source the
agents read (``web_fetch``) is captured into it, deduplicated by URL, so later runs can search what
earlier runs found. It is capped at ``MAX_LIBRARY_CHUNKS``; the least recently captured sources are
evicted first.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.core.errors import ArgusError
from argus.db.base import utcnow
from argus.db.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, MemoryItem
from argus.knowledge.bm25 import Doc, rank
from argus.knowledge.text import (
    TooLarge,
    UnsupportedType,
    chunk_text,
    detect_type,
    extract_text,
    snippet,
    term_counts,
    tokenize,
)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_KB_CHUNKS = 2_000
MAX_KBS_PER_OWNER = 100
MAX_SEARCH_CHUNKS = 50_000
MAX_K = 50
MAX_MEMORY_TEXT = 4_000
MAX_MEMORY_PER_SCOPE = 2_000
EXTRACT_TIMEOUT_S = 30
MAX_TEXT_CHARS = 1_000_000  # pasted text / fetched pages
LIBRARY_NAME = "Research library"
LIBRARY_DESCRIPTION = (
    "Captured automatically: every page and paper the agents read, with its source URL. "
    "Searchable by every run."
)
MAX_LIBRARY_CHUNKS = 20_000
MAX_CAPTURE_CHARS = 200_000  # per captured source


class LimitExceeded(ArgusError):
    status_code = 413
    code = "limit_exceeded"


class UnsupportedMedia(ArgusError):
    status_code = 415
    code = "unsupported_media_type"


# ------------------------------------------------------------------ knowledge bases
async def kb_stats(db: AsyncSession, kb_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    out = {kid: {"document_count": 0, "chunk_count": 0} for kid in kb_ids}
    if not kb_ids:
        return out
    rows = await db.execute(
        select(
            KnowledgeDocument.kb_id,
            func.count(KnowledgeDocument.id),
            func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0),
        )
        .where(KnowledgeDocument.kb_id.in_(kb_ids))
        .group_by(KnowledgeDocument.kb_id)
    )
    for kid, docs, chunks in rows:
        out[kid] = {"document_count": int(docs), "chunk_count": int(chunks)}
    return out


async def kb_chunk_count(db: AsyncSession, kb_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.kb_id == kb_id))
        ).scalar_one()
    )


async def ingest(
    db: AsyncSession,
    kb: KnowledgeBase,
    filename: str,
    declared_type: str | None,
    data: bytes,
    *,
    run_id: uuid.UUID | None = None,
) -> KnowledgeDocument:
    """Validate, extract, chunk and store one uploaded file. Caller commits."""
    filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()[:255]
    if not filename:
        raise UnsupportedMedia("A file name is required")
    if len(data) > MAX_FILE_BYTES:
        raise LimitExceeded(f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit")
    try:
        ctype = detect_type(filename, declared_type)
        # PDF parsing is CPU-bound: keep it off the event loop and bound how long a request waits.
        text = await asyncio.wait_for(asyncio.to_thread(extract_text, data, ctype), EXTRACT_TIMEOUT_S)
    except UnsupportedType as exc:
        raise UnsupportedMedia(str(exc)) from exc
    except TooLarge as exc:
        raise LimitExceeded(str(exc)) from exc
    except TimeoutError as exc:
        raise UnsupportedMedia("The document took too long to read") from exc
    return await _store(
        db,
        kb,
        text,
        filename=filename,
        content_type=ctype,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        origin="upload",
        run_id=run_id,
    )


def _max_chunks(kb: KnowledgeBase) -> int:
    return MAX_LIBRARY_CHUNKS if kb.kind == "library" else MAX_KB_CHUNKS


def _filename_for(title: str, url: str | None) -> str:
    base = title.strip() or (url or "").split("://", 1)[-1] or "note"
    safe = "".join(c if c.isalnum() or c in " -_.()" else "-" for c in base).strip(" -.")[:120]
    return (safe or "note") + ".txt"


async def _store(
    db: AsyncSession,
    kb: KnowledgeBase,
    text: str,
    *,
    filename: str,
    content_type: str,
    size: int,
    sha256: str,
    origin: str,
    title: str = "",
    source_url: str | None = None,
    run_id: uuid.UUID | None = None,
) -> KnowledgeDocument:
    pieces = chunk_text(text)
    if not pieces:
        raise ArgusError("The document contains no text")
    existing = await kb_chunk_count(db, kb.id)
    cap = _max_chunks(kb)
    if existing + len(pieces) > cap:
        if kb.kind == "library" and len(pieces) <= cap:
            await _evict_oldest(db, kb.id, existing + len(pieces) - cap)
        else:
            raise LimitExceeded(
                f"Knowledge base would exceed {cap} chunks "
                f"({existing} stored, this document adds {len(pieces)})"
            )
    doc = KnowledgeDocument(
        kb_id=kb.id,
        filename=filename[:255],
        content_type=content_type,
        size=size,
        sha256=sha256,
        chunk_count=len(pieces),
        title=title[:300],
        origin=origin,
        source_url=source_url,
        url_hash=_url_hash(source_url) if source_url else None,
        run_id=run_id,
    )
    db.add(doc)
    await db.flush()
    for i, piece in enumerate(pieces):
        terms, length = term_counts(piece)
        db.add(KnowledgeChunk(kb_id=kb.id, document_id=doc.id, idx=i, text=piece, length=length, terms=terms))
    await db.flush()
    return doc


async def _evict_oldest(db: AsyncSession, kb_id: uuid.UUID, chunks_needed: int) -> None:
    """Free at least ``chunks_needed`` chunks by deleting the least recently captured documents."""
    freed = 0
    rows = await db.execute(
        select(KnowledgeDocument.id, KnowledgeDocument.chunk_count)
        .where(KnowledgeDocument.kb_id == kb_id)
        .order_by(KnowledgeDocument.updated_at, KnowledgeDocument.id)
    )
    doomed = []
    for doc_id, count in rows:
        if freed >= chunks_needed:
            break
        doomed.append(doc_id)
        freed += count
    if doomed:
        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(doomed)))
        await db.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(doomed)))
        await db.flush()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()


async def ingest_text(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    title: str,
    text: str,
    origin: str = "text",
    source_url: str | None = None,
    run_id: uuid.UUID | None = None,
) -> KnowledgeDocument:
    """Store pasted text or an already-extracted page. Caller commits."""
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise LimitExceeded(f"Text exceeds {MAX_TEXT_CHARS:,} characters")
    data = text.encode()
    return await _store(
        db,
        kb,
        text,
        filename=_filename_for(title, source_url),
        content_type="text/plain",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        origin=origin,
        title=title.strip(),
        source_url=source_url,
        run_id=run_id,
    )


async def library_kb(db: AsyncSession, owner_id: uuid.UUID) -> KnowledgeBase:
    """The owner's research library, created on first use. Caller commits."""
    kb = (
        await db.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.owner_id == owner_id, KnowledgeBase.kind == "library")
            .order_by(KnowledgeBase.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if kb is not None:
        return kb
    name = LIBRARY_NAME
    taken = (
        await db.execute(
            select(KnowledgeBase.id).where(KnowledgeBase.owner_id == owner_id, KnowledgeBase.name == name)
        )
    ).first()
    if taken:
        name = f"{LIBRARY_NAME} (auto)"
    kb = KnowledgeBase(owner_id=owner_id, name=name, description=LIBRARY_DESCRIPTION, kind="library")
    db.add(kb)
    await db.flush()
    return kb


async def capture_source(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    url: str,
    title: str,
    text: str,
    run_id: uuid.UUID | None = None,
) -> KnowledgeDocument | None:
    """Put one source an agent read into the owner's research library. Caller commits.

    The same URL is stored once: unchanged content only refreshes it (so eviction keeps it), changed
    content replaces it. Returns None when there is nothing worth keeping.
    """
    text = text.strip()[:MAX_CAPTURE_CHARS]
    if len(text) < 200:
        return None
    kb = await library_kb(db, owner_id)
    sha = hashlib.sha256(text.encode()).hexdigest()
    old = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.kb_id == kb.id, KnowledgeDocument.url_hash == _url_hash(url)
            )
        )
    ).scalar_one_or_none()
    if old is not None:
        if old.sha256 == sha:
            old.run_id = run_id or old.run_id
            old.updated_at = utcnow()
            await db.flush()
            return old
        await db.delete(old)
        await db.flush()
    return await ingest_text(
        db, kb, title=title or url, text=text, origin="agent", source_url=url, run_id=run_id
    )


async def linked_summary(db: AsyncSession, owner_id: uuid.UUID, ids: Sequence[Any]) -> list[dict[str, Any]]:
    """Name, short description and size of the knowledge bases linked to a run (for agent prompts)."""
    wanted = []
    for i in ids:
        try:
            wanted.append(uuid.UUID(str(i)))
        except ValueError:
            continue
    if not wanted:
        return []
    kbs = list(
        (
            await db.execute(
                select(KnowledgeBase)
                .where(KnowledgeBase.owner_id == owner_id, KnowledgeBase.id.in_(wanted))
                .order_by(KnowledgeBase.name)
            )
        ).scalars()
    )
    stats = await kb_stats(db, [k.id for k in kbs])
    return [
        {
            "id": str(k.id),
            "name": k.name,
            "description": " ".join(k.description.split())[:300],
            "document_count": stats[k.id]["document_count"],
        }
        for k in kbs
    ]


async def search(db: AsyncSession, kb_ids: Sequence[uuid.UUID], query: str, k: int = 5) -> dict[str, Any]:
    """BM25-rank the chunks of ``kb_ids`` (already ownership-checked) for ``query``."""
    terms = list(dict.fromkeys(tokenize(query)))
    k = max(1, min(k, MAX_K))
    if not terms or not kb_ids:
        return {"query": query, "terms": terms, "results": []}
    rows = await db.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.terms, KnowledgeChunk.length)
        .where(KnowledgeChunk.kb_id.in_(list(kb_ids)))
        .limit(MAX_SEARCH_CHUNKS)
    )
    docs = [Doc(ref=cid, terms=t or {}, length=length or 0) for cid, t, length in rows]
    ranked = rank(terms, docs, k)
    if not ranked:
        return {"query": query, "terms": terms, "results": []}
    ids = [cid for _, cid in ranked]
    detail = {
        c.id: (c, d, b)
        for c, d, b in await db.execute(
            select(KnowledgeChunk, KnowledgeDocument, KnowledgeBase)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .join(KnowledgeBase, KnowledgeBase.id == KnowledgeChunk.kb_id)
            .where(KnowledgeChunk.id.in_(ids))
        )
    }
    results = []
    for score, cid in ranked:
        if cid not in detail:
            continue
        chunk, doc, kb = detail[cid]
        results.append(
            {
                "score": score,
                "kb_id": str(kb.id),
                "kb_name": kb.name,
                "document_id": str(doc.id),
                "filename": doc.filename,
                "title": doc.title,
                "source_url": doc.source_url,
                "chunk_index": chunk.idx,
                "text": chunk.text,
                "snippet": snippet(chunk.text, terms),
                "citation": f"{kb.name}/{doc.filename}#{chunk.idx}",
            }
        )
    return {"query": query, "terms": terms, "results": results}


# ------------------------------------------------------------------ memory
def memory_out(m: MemoryItem) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "council_id": str(m.council_id) if m.council_id else None,
        "key": m.key,
        "text": m.text,
        "tags": list(m.tags or []),
        "run_id": str(m.run_id) if m.run_id else None,
        "created_by": m.created_by,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def _scope(owner_id: uuid.UUID, council_id: uuid.UUID | None) -> list[Any]:
    cond: list[Any] = [MemoryItem.owner_id == owner_id]
    cond.append(MemoryItem.council_id == council_id if council_id else MemoryItem.council_id.is_(None))
    return cond


async def write_memory(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    council_id: uuid.UUID | None,
    text: str,
    key: str = "",
    tags: Sequence[str] = (),
    run_id: uuid.UUID | None = None,
    created_by: str = "",
) -> MemoryItem:
    """Insert a note, or update the note with the same non-empty ``key`` in the same scope."""
    text = text.strip()
    if not text:
        raise ArgusError("Memory text is required")
    if len(text) > MAX_MEMORY_TEXT:
        raise LimitExceeded(f"Memory text exceeds {MAX_MEMORY_TEXT} characters")
    key = key.strip()[:120]
    clean_tags = sorted({t.strip().lower()[:40] for t in tags if t.strip()})[:20]
    item = None
    if key:
        item = (
            await db.execute(select(MemoryItem).where(*_scope(owner_id, council_id), MemoryItem.key == key))
        ).scalar_one_or_none()
    if item is None:
        count = (
            await db.execute(select(func.count(MemoryItem.id)).where(*_scope(owner_id, council_id)))
        ).scalar_one()
        if count >= MAX_MEMORY_PER_SCOPE:
            raise LimitExceeded(f"Memory is full ({MAX_MEMORY_PER_SCOPE} items); delete some first")
        item = MemoryItem(owner_id=owner_id, council_id=council_id, key=key)
        db.add(item)
    item.text = text
    item.tags = clean_tags
    item.run_id = run_id
    item.created_by = created_by[:120]
    await db.flush()
    return item


async def search_memory(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    council_id: uuid.UUID | None,
    query: str,
    k: int = 5,
) -> list[tuple[float, MemoryItem]]:
    rows = list(
        (
            await db.execute(
                select(MemoryItem)
                .where(*_scope(owner_id, council_id))
                .order_by(MemoryItem.updated_at.desc())
                .limit(MAX_MEMORY_PER_SCOPE)
            )
        ).scalars()
    )
    k = max(1, min(k, MAX_K))
    terms = tokenize(query)
    if not terms:
        return [(0.0, m) for m in rows[:k]]
    docs = []
    for m in rows:
        counts, length = term_counts(" ".join([m.key, m.text, *(m.tags or [])]))
        docs.append(Doc(ref=m, terms=counts, length=length))
    return rank(terms, docs, k)
