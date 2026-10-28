"""Coding tools: a versioned file workspace plus command execution in the sandbox."""

from __future__ import annotations

import fnmatch
import re

from pydantic import BaseModel, Field

from argus.sandbox.client import Sandbox
from argus.tools.base import Tool, ToolCallContext, ToolResult

MAX_READ = 100_000


class FsRead(Tool):
    name = "fs_read"
    description = "Read a workspace file. Optionally a line range (1-based, inclusive)."

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)
        start_line: int | None = Field(default=None, ge=1)
        end_line: int | None = Field(default=None, ge=1)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        art = await ctx.run.workspace.read(args.path)
        if art is None or art.content is None:
            return ToolResult.error(f"No such text file: {args.path}")
        lines = art.content.splitlines()
        lo = (args.start_line or 1) - 1
        hi = args.end_line or len(lines)
        body = "\n".join(f"{i + 1:>5}  {line}" for i, line in enumerate(lines[lo:hi], start=lo))
        return ToolResult(body[:MAX_READ] + ("\n[truncated]" if len(body) > MAX_READ else ""))


class FsWrite(Tool):
    name = "fs_write"
    description = "Create or overwrite a workspace file with the full content."
    risk = frozenset({"fs_write"})

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)
        content: str = Field(max_length=5_000_000)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        art = await ctx.run.workspace.write(args.path, args.content, by=ctx.member_name)
        return ToolResult(f"Wrote {art.path} (v{art.version}, {len(args.content.splitlines())} lines)")


class FsEdit(Tool):
    name = "fs_edit"
    description = (
        "Replace an exact snippet in a workspace file. "
        "'old' must appear exactly once unless replace_all is true."
    )
    risk = frozenset({"fs_write"})

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)
        old: str = Field(min_length=1, max_length=200_000)
        new: str = Field(max_length=200_000)
        replace_all: bool = False

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        art = await ctx.run.workspace.read(args.path)
        if art is None or art.content is None:
            return ToolResult.error(f"No such text file: {args.path}")
        count = art.content.count(args.old)
        if count == 0:
            return ToolResult.error("'old' snippet not found; read the file and copy the text exactly")
        if count > 1 and not args.replace_all:
            return ToolResult.error(f"'old' appears {count} times; add context or set replace_all")
        updated = (
            art.content.replace(args.old, args.new)
            if args.replace_all
            else art.content.replace(args.old, args.new, 1)
        )
        new = await ctx.run.workspace.write(args.path, updated, by=ctx.member_name)
        return ToolResult(
            f"Edited {new.path} (v{new.version}, {count if args.replace_all else 1} replacement(s))"
        )


class FsList(Tool):
    name = "fs_list"
    description = "List workspace files, optionally filtered by a glob such as 'src/**/*.py'."

    class Args(BaseModel):
        pattern: str = Field(default="*", max_length=200)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        files = [
            f
            for f in await ctx.run.workspace.list()
            if fnmatch.fnmatch(f["path"], args.pattern) or args.pattern in ("*", "**", "**/*")
        ]
        if not files:
            return ToolResult("No matching files.")
        return ToolResult("\n".join(f"{f['path']}  ({f['size']} B, v{f['version']})" for f in files))


class FsSearch(Tool):
    name = "fs_search"
    description = "Search workspace text files with a regular expression; returns path:line matches."

    class Args(BaseModel):
        regex: str = Field(min_length=1, max_length=500)
        pattern: str = Field(default="*", max_length=200)
        max_results: int = Field(default=100, ge=1, le=1000)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        try:
            rx = re.compile(args.regex)
        except re.error as exc:
            return ToolResult.error(f"Invalid regex: {exc}")
        hits: list[str] = []
        for path, content in (await ctx.run.workspace.text_files()).items():
            if not fnmatch.fnmatch(path, args.pattern):
                continue
            for n, line in enumerate(content.splitlines(), 1):
                if rx.search(line[:2000]):
                    hits.append(f"{path}:{n}: {line.strip()[:200]}")
                    if len(hits) >= args.max_results:
                        return ToolResult("\n".join(hits) + "\n[more matches omitted]")
        return ToolResult("\n".join(hits) or "No matches.")


class FsDelete(Tool):
    name = "fs_delete"
    description = "Delete a workspace file (history is kept)."
    risk = frozenset({"fs_write"})

    class Args(BaseModel):
        path: str = Field(min_length=1, max_length=400)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        ok = await ctx.run.workspace.delete(args.path, by=ctx.member_name)
        return ToolResult(f"Deleted {args.path}") if ok else ToolResult.error(f"No such file: {args.path}")


class Shell(Tool):
    name = "shell"
    description = (
        "Run a shell command in an isolated sandbox containing the workspace files (no network). "
        "Files created or modified by the command are saved back to the workspace. "
        "Use it to run code and tests."
    )
    risk = frozenset({"shell"})

    class Args(BaseModel):
        command: str = Field(min_length=1, max_length=10_000)
        timeout_s: int = Field(default=60, ge=1, le=600)

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def timeout_for(self, args: BaseModel | None) -> float | None:
        # The command's own limit plus room to sync the workspace before and after.
        limit = (
            args.timeout_s if isinstance(args, Shell.Args) else self.Args.model_fields["timeout_s"].default
        )
        return float(limit) + 60

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        files = await ctx.run.workspace.text_files()
        try:
            res = await self.sandbox.run(files, args.command, args.timeout_s)
        except Exception as exc:
            return ToolResult.error(f"Sandbox error: {str(exc)[:300]}")
        saved = []
        for path, content in res.get("changed", {}).items():
            try:
                await ctx.run.workspace.write(path, content, by=ctx.member_name)
                saved.append(path)
            except Exception as exc:
                saved.append(f"{path} (not saved: {str(exc)[:80]})")
        for path in res.get("deleted", []):
            await ctx.run.workspace.delete(path, by=ctx.member_name)
        if ctx.emit_output:
            for stream in ("stdout", "stderr"):
                if res.get(stream):
                    await ctx.emit_output(stream, res[stream][-8000:])
        parts = [
            f"$ {args.command}",
            f"exit code: {res.get('exit_code')}" + (" (timed out)" if res.get("timed_out") else ""),
        ]
        if res.get("stdout"):
            parts.append("stdout:\n" + res["stdout"][-20_000:])
        if res.get("stderr"):
            parts.append("stderr:\n" + res["stderr"][-10_000:])
        if saved:
            parts.append("files saved: " + ", ".join(saved))
        if res.get("deleted"):
            parts.append("files deleted: " + ", ".join(res["deleted"]))
        return ToolResult("\n".join(parts), is_error=res.get("exit_code") != 0)


def code_tools(sandbox: Sandbox | None) -> list[Tool]:
    tools: list[Tool] = [FsRead(), FsWrite(), FsEdit(), FsList(), FsSearch(), FsDelete()]
    if sandbox is not None:
        tools.append(Shell(sandbox))
    return tools
