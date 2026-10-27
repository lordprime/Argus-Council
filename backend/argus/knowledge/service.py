"""Knowledge-base ingestion and BM25 search, plus council-scoped long-term memory."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.core.errors import ArgusError
from argus.db.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, MemoryItem
from argus.knowledge.bm25 import Doc, rank
from argus.knowledge.text import (
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
    db: AsyncSession, kb: KnowledgeBase, filename: str, declared_type: str | None, data: bytes
) -> KnowledgeDocument:
    """Validate, extract, chunk and store one document. Caller commits."""
    filename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()[:255]
    if not filename:
        raise UnsupportedMedia("A file name is required")
    if len(data) > MAX_FILE_BYTES:
        raise LimitExceeded(f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit")
    try:
        ctype = detect_type(filename, declared_type)
        text = extract_text(data, ctype)
    except UnsupportedType as exc:
        raise UnsupportedMedia(str(exc)) from exc
    pieces = chunk_text(text)
    if not pieces:
        raise ArgusError("The document contains no text")
    existing = await kb_chunk_count(db, kb.id)
    if existing + len(pieces) > MAX_KB_CHUNKS:
        raise LimitExceeded(
            f"Knowledge base would exceed {MAX_KB_CHUNKS} chunks "
            f"({existing} stored, this document adds {len(pieces)})"
        )
    doc = KnowledgeDocument(
        kb_id=kb.id,
        filename=filename,
        content_type=ctype,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        chunk_count=len(pieces),
    )
    db.add(doc)
    await db.flush()
    for i, piece in enumerate(pieces):
        terms, length = term_counts(piece)
        db.add(KnowledgeChunk(kb_id=kb.id, document_id=doc.id, idx=i, text=piece, length=length, terms=terms))
    await db.flush()
    return doc


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
