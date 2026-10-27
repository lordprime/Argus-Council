"""Knowledge bases (documents + BM25 search) and long-term memory items."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep, write_scope
from argus.core.errors import Conflict, NotFound
from argus.db.models import Council, KnowledgeBase, KnowledgeDocument, MemoryItem
from argus.knowledge import service
from argus.knowledge.service import MAX_FILE_BYTES, MAX_KB_CHUNKS, LimitExceeded

router = APIRouter(tags=["knowledge"], dependencies=[write_scope("knowledge:write")])


class KnowledgeBaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class KnowledgeBasePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class MemoryIn(BaseModel):
    text: str = Field(min_length=1, max_length=service.MAX_MEMORY_TEXT)
    key: str = Field(default="", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=20)
    council_id: uuid.UUID | None = None


def _kb_out(kb: KnowledgeBase, stats: dict[str, int]) -> dict:
    return {
        "id": str(kb.id),
        "name": kb.name,
        "description": kb.description,
        "document_count": stats.get("document_count", 0),
        "chunk_count": stats.get("chunk_count", 0),
        "max_chunks": MAX_KB_CHUNKS,
        "created_at": kb.created_at.isoformat(),
        "updated_at": kb.updated_at.isoformat(),
    }


def _doc_out(d: KnowledgeDocument) -> dict:
    return {
        "id": str(d.id),
        "kb_id": str(d.kb_id),
        "filename": d.filename,
        "content_type": d.content_type,
        "size": d.size,
        "sha256": d.sha256,
        "chunk_count": d.chunk_count,
        "created_at": d.created_at.isoformat(),
    }


async def _owned_kb(db, owner_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None or kb.owner_id != owner_id:
        raise NotFound("Knowledge base not found")
    return kb


async def _name_taken(db, owner_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None) -> bool:
    q = select(KnowledgeBase.id).where(KnowledgeBase.owner_id == owner_id, KnowledgeBase.name == name)
    if exclude is not None:
        q = q.where(KnowledgeBase.id != exclude)
    return (await db.execute(q)).first() is not None


# ------------------------------------------------------------------ knowledge bases
@router.get("/knowledge-bases")
async def list_kbs(principal: CurrentPrincipal, db: DbDep) -> dict:
    kbs = list(
        (
            await db.execute(
                select(KnowledgeBase)
                .where(KnowledgeBase.owner_id == principal.id)
                .order_by(KnowledgeBase.name)
            )
        ).scalars()
    )
    stats = await service.kb_stats(db, [k.id for k in kbs])
    return {"knowledge_bases": [_kb_out(k, stats[k.id]) for k in kbs]}


@router.post("/knowledge-bases", status_code=status.HTTP_201_CREATED)
async def create_kb(data: KnowledgeBaseIn, principal: CurrentPrincipal, db: DbDep) -> dict:
    name = data.name.strip()
    if await _name_taken(db, principal.id, name):
        raise Conflict(f"You already have a knowledge base named '{name}'")
    count = len(
        (await db.execute(select(KnowledgeBase.id).where(KnowledgeBase.owner_id == principal.id))).all()
    )
    if count >= service.MAX_KBS_PER_OWNER:
        raise LimitExceeded(f"At most {service.MAX_KBS_PER_OWNER} knowledge bases per account")
    kb = KnowledgeBase(owner_id=principal.id, name=name, description=data.description)
    db.add(kb)
    await db.flush()
    await db.refresh(kb)
    return _kb_out(kb, {})


@router.get("/knowledge-bases/search")
async def search_all(
    principal: CurrentPrincipal,
    db: DbDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    k: Annotated[int, Query(ge=1, le=service.MAX_K)] = 10,
    kb: Annotated[list[uuid.UUID] | None, Query()] = None,
) -> dict:
    """Search across the caller's knowledge bases, optionally filtered by ``kb`` ids."""
    q_ids = select(KnowledgeBase.id).where(KnowledgeBase.owner_id == principal.id)
    if kb:
        q_ids = q_ids.where(KnowledgeBase.id.in_(kb))
    ids = [r[0] for r in await db.execute(q_ids)]
    return await service.search(db, ids, q, k)


@router.get("/knowledge-bases/{kb_id}")
async def get_kb(kb_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> dict:
    kb = await _owned_kb(db, principal.id, kb_id)
    stats = await service.kb_stats(db, [kb.id])
    return _kb_out(kb, stats[kb.id])


@router.patch("/knowledge-bases/{kb_id}")
async def update_kb(
    kb_id: uuid.UUID, data: KnowledgeBasePatch, principal: CurrentPrincipal, db: DbDep
) -> dict:
    kb = await _owned_kb(db, principal.id, kb_id)
    if data.name is not None:
        name = data.name.strip()
        if await _name_taken(db, principal.id, name, exclude=kb.id):
            raise Conflict(f"You already have a knowledge base named '{name}'")
        kb.name = name
    if data.description is not None:
        kb.description = data.description
    await db.flush()
    await db.refresh(kb)
    stats = await service.kb_stats(db, [kb.id])
    return _kb_out(kb, stats[kb.id])


@router.delete("/knowledge-bases/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb(kb_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> Response:
    kb = await _owned_kb(db, principal.id, kb_id)
    await db.delete(kb)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ documents
@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> dict:
    kb = await _owned_kb(db, principal.id, kb_id)
    docs = (
        await db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.kb_id == kb.id)
            .order_by(KnowledgeDocument.created_at.desc())
        )
    ).scalars()
    return {"documents": [_doc_out(d) for d in docs]}


@router.post("/knowledge-bases/{kb_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep, file: UploadFile = File(...)
) -> dict:
    kb = await _owned_kb(db, principal.id, kb_id)
    data = await file.read(MAX_FILE_BYTES + 1)
    doc = await service.ingest(db, kb, file.filename or "", file.content_type, data)
    await db.refresh(doc)
    return _doc_out(doc)


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    kb_id: uuid.UUID, doc_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep
) -> Response:
    kb = await _owned_kb(db, principal.id, kb_id)
    doc = await db.get(KnowledgeDocument, doc_id)
    if doc is None or doc.kb_id != kb.id:
        raise NotFound("Document not found")
    await db.delete(doc)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/knowledge-bases/{kb_id}/search")
async def search_kb(
    kb_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DbDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    k: Annotated[int, Query(ge=1, le=service.MAX_K)] = 10,
) -> dict:
    kb = await _owned_kb(db, principal.id, kb_id)
    return await service.search(db, [kb.id], q, k)


# ------------------------------------------------------------------ memory
@router.get("/memory")
async def list_memory(
    principal: CurrentPrincipal,
    db: DbDep,
    council_id: uuid.UUID | None = None,
    scope: Annotated[str, Query(pattern="^(all|council|global)$")] = "all",
    q: Annotated[str, Query(max_length=500)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    """List memory items. ``council_id`` narrows to one council; ``scope=global`` to council-less items."""
    if q.strip() and (council_id or scope == "global"):
        ranked = await service.search_memory(
            db, owner_id=principal.id, council_id=council_id, query=q, k=min(limit, service.MAX_K)
        )
        return {"items": [{**service.memory_out(m), "score": s} for s, m in ranked]}
    stmt = select(MemoryItem).where(MemoryItem.owner_id == principal.id)
    if council_id is not None:
        stmt = stmt.where(MemoryItem.council_id == council_id)
    elif scope == "global":
        stmt = stmt.where(MemoryItem.council_id.is_(None))
    elif scope == "council":
        stmt = stmt.where(MemoryItem.council_id.is_not(None))
    rows = list((await db.execute(stmt.order_by(MemoryItem.updated_at.desc()).limit(limit))).scalars())
    if q.strip():
        needle = q.strip().lower()
        rows = [m for m in rows if needle in f"{m.key} {m.text} {' '.join(m.tags or [])}".lower()]
    return {"items": [service.memory_out(m) for m in rows]}


@router.post("/memory", status_code=status.HTTP_201_CREATED)
async def create_memory(data: MemoryIn, principal: CurrentPrincipal, db: DbDep) -> dict:
    if data.council_id is not None:
        council = await db.get(Council, data.council_id)
        if council is None or council.owner_id != principal.id:
            raise NotFound("Council not found")
    item = await service.write_memory(
        db,
        owner_id=principal.id,
        council_id=data.council_id,
        text=data.text,
        key=data.key,
        tags=data.tags,
        created_by="user",
    )
    await db.refresh(item)
    return service.memory_out(item)


@router.delete("/memory/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(item_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep) -> Response:
    item = await db.get(MemoryItem, item_id)
    if item is None or item.owner_id != principal.id:
        raise NotFound("Memory item not found")
    await db.delete(item)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
