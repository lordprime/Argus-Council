"""Agent tools for knowledge-base search and council-scoped long-term memory.

Which knowledge bases a run may search: the council snapshot's ``knowledge_base_ids`` (ids or names)
when present, otherwise every knowledge base the run's owner has. Memory is scoped to the run's
council (or to the owner's council-less memory for ad-hoc runs).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from argus.core.errors import ArgusError
from argus.db.models import KnowledgeBase, Run
from argus.knowledge import service
from argus.tools.base import Tool, ToolCallContext, ToolResult

_SCOPE_KEY = "knowledge_scope"


async def _scope(ctx: ToolCallContext) -> dict[str, Any]:
    """Resolve (once per run) the council id and the knowledge bases this run may read."""
    cached = ctx.run.extra.get(_SCOPE_KEY)
    if cached is not None:
        return cached
    async with ctx.run.sessionmaker() as s:
        run = await s.get(Run, ctx.run.run_id)
        snapshot = (run.council_snapshot or {}) if run else {}
        council_id = run.council_id if run else None
        kbs = list(
            (
                await s.execute(select(KnowledgeBase).where(KnowledgeBase.owner_id == ctx.run.owner_id))
            ).scalars()
        )
    allowed = snapshot.get("knowledge_base_ids")
    if isinstance(allowed, list):
        wanted = {str(a) for a in allowed}
        kbs = [k for k in kbs if str(k.id) in wanted or k.name in wanted]
    scope = {"council_id": council_id, "kbs": {k.name: k.id for k in kbs}}
    ctx.run.extra[_SCOPE_KEY] = scope
    return scope


class KnowledgeSearch(Tool):
    name = "knowledge_search"
    description = (
        "Search the user's uploaded knowledge bases (documents) for passages relevant to a query. "
        "Returns ranked passages with citations [knowledge base/document#chunk]; cite them when used."
    )

    class Args(BaseModel):
        query: str = Field(min_length=1, max_length=500)
        k: int = Field(default=5, ge=1, le=20)
        knowledge_bases: list[str] | None = Field(
            default=None, max_length=20, description="Restrict to these knowledge base names"
        )

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        scope = await _scope(ctx)
        kbs: dict[str, uuid.UUID] = scope["kbs"]
        if not kbs:
            return ToolResult.error("No knowledge bases are available to this run")
        if args.knowledge_bases:
            unknown = [n for n in args.knowledge_bases if n not in kbs]
            if unknown:
                return ToolResult.error(
                    f"Unknown or not permitted knowledge base(s): {', '.join(unknown)}. "
                    f"Available: {', '.join(sorted(kbs))}"
                )
            ids = [kbs[n] for n in args.knowledge_bases]
        else:
            ids = list(kbs.values())
        async with ctx.run.sessionmaker() as s:
            found = await service.search(s, ids, args.query, args.k)
        results = found["results"]
        if not results:
            return ToolResult(
                f"No passages matched '{args.query}' in: {', '.join(sorted(kbs))}",
                data={"results": [], "terms": found["terms"]},
            )
        lines = []
        sources = ctx.run.sources
        for i, r in enumerate(results, 1):
            url = f"kb://{r['kb_name']}/{r['filename']}#chunk-{r['chunk_index']}"
            source = sources.get(url)
            if source is None:
                source = {
                    "source_id": f"src_{len(sources) + 1}",
                    "n": len(sources) + 1,
                    "url": url,
                    "title": f"{r['filename']} ({r['kb_name']}, chunk {r['chunk_index']})",
                    "retrieved_at": datetime.now(UTC).isoformat(),
                }
                sources[url] = source
                await ctx.run.sink.emit(
                    "citation.added",
                    {**source, "snippet": r["snippet"][:280], "claim_refs": []},
                    step_id=ctx.step_id,
                )
            lines.append(f"[{i}] [Source {source['n']}] {r['citation']} (score {r['score']})\n{r['text']}")
        citations = [
            {k: r[k] for k in ("kb_name", "kb_id", "document_id", "filename", "chunk_index", "score")}
            for r in results
        ]
        return ToolResult("\n\n".join(lines), data={"results": citations, "terms": found["terms"]})


class MemoryWrite(Tool):
    name = "memory_write"
    description = (
        "Save a durable note to this council's long-term memory so future runs can recall it. "
        "Use a short `key` to update an existing note instead of duplicating it."
    )

    class Args(BaseModel):
        text: str = Field(min_length=1, max_length=service.MAX_MEMORY_TEXT)
        key: str = Field(default="", max_length=120)
        tags: list[str] = Field(default_factory=list, max_length=20)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        scope = await _scope(ctx)
        try:
            async with ctx.run.sessionmaker() as s:
                item = await service.write_memory(
                    s,
                    owner_id=ctx.run.owner_id,
                    council_id=scope["council_id"],
                    text=args.text,
                    key=args.key,
                    tags=args.tags,
                    run_id=ctx.run.run_id,
                    created_by=ctx.member_name,
                )
                await s.commit()
                item_id = str(item.id)
        except ArgusError as exc:
            return ToolResult.error(exc.message)
        return ToolResult(
            f"Saved memory {item_id}" + (f" (key '{args.key}')" if args.key else ""),
            data={"memory_id": item_id, "key": args.key},
        )


class MemorySearch(Tool):
    name = "memory_search"
    description = (
        "Recall notes from this council's long-term memory (saved in earlier runs). "
        "An empty query returns the most recent notes."
    )

    class Args(BaseModel):
        query: str = Field(default="", max_length=500)
        k: int = Field(default=5, ge=1, le=20)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        scope = await _scope(ctx)
        async with ctx.run.sessionmaker() as s:
            ranked = await service.search_memory(
                s, owner_id=ctx.run.owner_id, council_id=scope["council_id"], query=args.query, k=args.k
            )
        if not ranked:
            return ToolResult("No matching memories.", data={"items": []})
        lines = []
        for _score, m in ranked:
            label = f"[memory {m.id}" + (f" key={m.key}" if m.key else "") + "]"
            tags = f" tags: {', '.join(m.tags)}" if m.tags else ""
            lines.append(f"{label}{tags}\n{m.text}")
        return ToolResult(
            "\n\n".join(lines),
            data={"items": [{"id": str(m.id), "key": m.key, "score": score} for score, m in ranked]},
        )


def knowledge_tools() -> list[Tool]:
    return [KnowledgeSearch(), MemoryWrite(), MemorySearch()]
