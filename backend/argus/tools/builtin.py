"""Built-in council tools: shared notes and the run's artifact workspace."""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from argus.tools.base import StrList, Tool, ToolCallContext, ToolResult


class NotesWrite(Tool):
    name = "notes_write"
    # The council board: every seat may read and post, whatever its role's tool list says.
    source = "council"
    description = (
        "Post a note to the council board, which every member sees at every turn. Use for findings, "
        "decisions and evidence others should build on. Include the source URL in 'source' when the "
        "note comes from a document or web page."
    )

    class Args(BaseModel):
        title: str = Field(max_length=200)
        body: str = Field(max_length=20_000)
        source: str | None = Field(default=None, max_length=2000)
        tags: StrList = Field(default_factory=list, max_length=10)

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
        bus = ctx.run.extra.get("council_bus")
        author = bus.members.get(ctx.member_id) if bus is not None else None
        if author is not None:
            await bus.record_board(author, note, step_id=ctx.step_id)  # an A2A board post
        return ToolResult(
            f"Saved note #{note['id']} on the council board: {args.title}", data={"note_id": note["id"]}
        )


class NotesRead(Tool):
    name = "notes_read"
    parallel_safe = True
    # The council board: every seat may read and post, whatever its role's tool list says.
    source = "council"
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


def workspace_path(path: str, root: str = "/workspace") -> str:
    """``path`` as the workspace stores it: without ``./``, a leading ``/``, the sandbox root or a
    leading ``workspace/`` (the shapes models write paths in)."""
    rel = path.strip().replace("\\", "/")
    root = root.rstrip("/")
    while True:
        before = rel
        if root and (rel == root or rel.startswith(root + "/")):
            rel = rel[len(root) :]
        rel = rel.lstrip("/")
        while rel.startswith("./"):
            rel = rel[2:].lstrip("/")
        if rel.startswith("workspace/"):
            rel = rel[len("workspace/") :]
        if rel == before:
            return rel


def match_path(path: str, files: list[dict[str, Any]]) -> str | None:
    """The workspace file ``path`` most likely means: same path in any case, then the only file
    with that name in any folder, then a close spelling."""
    paths = [f["path"] for f in files]
    low = path.lower()
    same = [p for p in paths if p.lower() == low]
    if same:
        return same[0]
    name = low.rsplit("/", 1)[-1]
    by_name = [p for p in paths if p.lower().rsplit("/", 1)[-1] == name]
    if len(by_name) == 1:
        return by_name[0]
    close = difflib.get_close_matches(low, [p.lower() for p in paths], n=1, cutoff=0.8)
    return next((p for p in paths if close and p.lower() == close[0]), None)


class ArtifactRead(Tool):
    name = "artifact_read"
    parallel_safe = True
    description = (
        "Read a file from the run workspace (paths as artifact_list shows them, e.g. 'report.md'). "
        "Only files written in this run exist: use artifact_list to see them."
    )

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)
        version: int | None = Field(default=None, ge=1)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        workspace = ctx.run.workspace
        art = await workspace.read(args.path, args.version)
        rel = workspace_path(args.path, ctx.run.engine.rails.sandbox.root) or args.path
        if art is None and rel != args.path.strip():
            art = await workspace.read(rel, args.version)
        if art is not None:
            return ToolResult(art.content or "", data={"path": art.path})
        files = await workspace.list()
        if not files:
            return ToolResult.error(
                f"No such file: {args.path}. The workspace is empty: nothing has been written in this "
                "run yet, so a file must be created with artifact_write before it can be read."
            )
        guess = match_path(rel, files)
        if guess is not None:
            art = await workspace.read(guess, args.version)
            if art is not None:
                return ToolResult(
                    f"[Read {art.path}, the closest match for '{args.path}']\n{art.content or ''}",
                    data={"path": art.path},
                )
            if args.version:
                return ToolResult.error(
                    f"{guess} has no version {args.version}; leave out `version` for the latest."
                )
        listing = "\n".join(f"- {f['path']}" for f in files[:50])
        more = f"\n(and {len(files) - 50} more)" if len(files) > 50 else ""
        return ToolResult.error(f"No such file: {args.path}. Files in the workspace:\n{listing}{more}")


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
