"""Built-in council tools: shared notes and the run's artifact workspace."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from argus.tools.base import Tool, ToolCallContext, ToolResult


class NotesWrite(Tool):
    name = "notes_write"
    description = (
        "Add a note to the council's shared scratchpad. Use for findings, decisions and evidence. "
        "Include the source URL in 'source' when the note comes from a document or web page."
    )

    class Args(BaseModel):
        title: str = Field(max_length=200)
        body: str = Field(max_length=20_000)
        source: str | None = Field(default=None, max_length=2000)
        tags: list[str] = Field(default_factory=list, max_length=10)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        note = {
            "id": len(ctx.run.notes) + 1,
            "title": args.title,
            "body": args.body,
            "source": args.source,
            "tags": args.tags,
            "author": ctx.member_name,
            "ts": datetime.now(UTC).isoformat(),
        }
        ctx.run.notes.append(note)
        return ToolResult(f"Saved note #{note['id']}: {args.title}", data={"note_id": note["id"]})


class NotesRead(Tool):
    name = "notes_read"
    description = "Read the council's shared notes, optionally filtered by a keyword or tag."

    class Args(BaseModel):
        query: str | None = Field(default=None, max_length=200)
        limit: int = Field(default=30, ge=1, le=200)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        notes = ctx.run.notes
        if args.query:
            q = args.query.lower()
            notes = [n for n in notes if q in (n["title"] + n["body"]).lower() or q in n["tags"]]
        notes = notes[-args.limit :]
        if not notes:
            return ToolResult("No notes yet.")
        lines = [
            f"#{n['id']} [{n['author']}] {n['title']}\n{n['body']}"
            + (f"\nSource: {n['source']}" if n["source"] else "")
            for n in notes
        ]
        return ToolResult("\n\n".join(lines))


class ArtifactWrite(Tool):
    name = "artifact_write"
    description = (
        "Create or overwrite a file in the run workspace (a new version is kept). "
        "Paths are relative, e.g. 'report.md' or 'src/app.py'."
    )
    risk = frozenset({"fs_write"})

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)
        content: str = Field(max_length=5_000_000)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        art = await ctx.run.workspace.write(args.path, args.content, by=ctx.member_name)
        return ToolResult(f"Wrote {art.path} (v{art.version}, {art.size} bytes)", data={"path": art.path})


class ArtifactRead(Tool):
    name = "artifact_read"
    description = "Read a file from the run workspace."

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)
        version: int | None = Field(default=None, ge=1)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        art = await ctx.run.workspace.read(args.path, args.version)
        if art is None:
            return ToolResult.error(f"No such file: {args.path}")
        return ToolResult(art.content or "")


class ArtifactList(Tool):
    name = "artifact_list"
    description = "List files in the run workspace."

    class Args(BaseModel):
        pass

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        files = await ctx.run.workspace.list()
        if not files:
            return ToolResult("Workspace is empty.")
        return ToolResult("\n".join(f"{f['path']}  v{f['version']}  {f['size']}B" for f in files))


def builtin_tools() -> list[Tool]:
    return [NotesWrite(), NotesRead(), ArtifactWrite(), ArtifactRead(), ArtifactList()]
