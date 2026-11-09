"""Tools that let agents discover and load skills on demand."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from argus.core.logging import get_logger
from argus.runtime.guardrails import GuardrailBreach
from argus.tools.base import Tool, ToolCallContext, ToolResult
from argus.tools.remote import record_call

log = get_logger(__name__)


async def save_skill_text(ctx: ToolCallContext, path: str, text: str) -> tuple[str | None, str]:
    """Save the exact text an agent received as a run artifact, once per run (per distinct content).

    Returns (artifact_path or None, sha256). Never raises: a failed save must not fail the skill call.
    """
    sha = hashlib.sha256(text.encode()).hexdigest()
    saved: dict[str, str] = ctx.run.extra.setdefault("skill_artifacts", {})
    if saved.get(path) == sha:
        return path, sha
    try:
        await ctx.run.workspace.write(path, text, by=ctx.member_id, kind="skill")
    except GuardrailBreach as exc:
        log.info("skill.artifact_failed", path=path, error=str(exc))
        return None, sha
    except Exception as exc:  # tracing must never break the skill call
        log.warning("skill.artifact_failed", path=path, error=type(exc).__name__)
        return None, sha
    saved[path] = sha
    return path, sha


def mark_active(ctx: ToolCallContext, name: str) -> None:
    """Record that this member has the skill in effect (from its use_skill call until the run ends)."""
    active: list[str] = ctx.run.extra.setdefault("active_skills", {}).setdefault(ctx.member_id, [])
    if name not in active:
        active.append(name)


async def ledger_skill(
    ctx: ToolCallContext,
    method: str,
    name: str,
    version: str | None,
    path: str,
    request: dict,
    response: dict,
    status: str,
) -> None:
    """Write the ledger row for a skill call. A failure is logged, never raised."""
    try:
        await record_call(
            ctx,
            protocol="skill",
            direction="internal",
            peer=f"{name}@{version or 'unknown'}",
            method=method,
            status=status,
            request=request,
            response=response,
            detail={"member_id": ctx.member_id, "path": path},
        )
    except Exception as exc:  # the ledger is a record, never a dependency
        log.warning("skill.ledger_failed", method=method, error=type(exc).__name__)


class UseSkill(Tool):
    name = "use_skill"
    description = (
        "Load a skill's full instructions by name (see 'Available skills' in your instructions). "
        "Load a skill before doing a task it covers, then follow its instructions."
    )
    source = "skill"

    class Args(BaseModel):
        name: str = Field(max_length=64)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        pkg = ctx.run.skills.get(args.name)
        if pkg is None:
            available = ", ".join(sorted(ctx.run.skills)) or "none"
            err = f"No skill named '{args.name}'. Available: {available}"
            await ledger_skill(
                ctx,
                "use_skill",
                args.name,
                None,
                f"skills/{args.name}/SKILL.md",
                args.model_dump(),
                {"error": err},
                "error",
            )
            return ToolResult.error(err)
        files = "\n".join(f"- {p}" for p in sorted(pkg.files)) or "(none)"
        tools = ", ".join(pkg.allowed_tools) or "any permitted tool"
        text = (
            f"# Skill: {pkg.name} (v{pkg.version})\n\n## When to use\n{pkg.description}\n\n"
            f"## Instructions\n{pkg.body}\n\n"
            f"---\nBundled resources (read with skill_read):\n{files}\nTools this skill expects: {tools}"
        )
        artifact_path, sha = await save_skill_text(ctx, f"skills/{pkg.name}/SKILL.md", text)
        mark_active(ctx, pkg.name)
        await ledger_skill(
            ctx,
            "use_skill",
            pkg.name,
            pkg.version,
            f"skills/{pkg.name}/SKILL.md",
            args.model_dump(),
            {"sha256": sha, "bytes": len(text.encode())},
            "ok",
        )
        await ctx.run.sink.emit(
            "skill.loaded",
            {
                "skill_id": pkg.name,
                "name": pkg.name,
                "member_id": ctx.member_id,
                "version": pkg.version,
                "tool_call_id": ctx.tool_call_id,
                "artifact_path": artifact_path,
                "sha256": sha,
                "bytes": len(text.encode()),
                "files": sorted(pkg.files),
                "expected_tools": list(pkg.allowed_tools),
            },
        )
        return ToolResult(text, data={"skill": pkg.name})


class SkillRead(Tool):
    name = "skill_read"
    description = "Read a resource file bundled with a skill (templates, references, scripts)."
    source = "skill"

    class Args(BaseModel):
        name: str = Field(max_length=64)
        path: str = Field(max_length=300)

    async def run(self, args: Args, ctx: ToolCallContext) -> ToolResult:
        pkg = ctx.run.skills.get(args.name)
        if pkg is None:
            err = f"No skill named '{args.name}'"
            await ledger_skill(
                ctx,
                "skill_read",
                args.name,
                None,
                args.path.lstrip("./"),
                args.model_dump(),
                {"error": err},
                "error",
            )
            return ToolResult.error(err)
        rel = args.path.lstrip("./")
        content = pkg.files.get(rel)
        if content is None:
            err = f"'{args.path}' not found in skill {pkg.name}. Files: {', '.join(sorted(pkg.files))}"
            await ledger_skill(
                ctx,
                "skill_read",
                pkg.name,
                pkg.version,
                f"skills/{pkg.name}/{rel}",
                args.model_dump(),
                {"error": err},
                "error",
            )
            return ToolResult.error(err)
        artifact_path, sha = await save_skill_text(ctx, f"skills/{pkg.name}/{rel}", content)
        await ledger_skill(
            ctx,
            "skill_read",
            pkg.name,
            pkg.version,
            f"skills/{pkg.name}/{rel}",
            args.model_dump(),
            {"sha256": sha, "bytes": len(content.encode())},
            "ok",
        )
        await ctx.run.sink.emit(
            "skill.read",
            {
                "name": pkg.name,
                "version": pkg.version,
                "member_id": ctx.member_id,
                "tool_call_id": ctx.tool_call_id,
                "path": rel,
                "artifact_path": artifact_path,
                "sha256": sha,
                "bytes": len(content.encode()),
            },
        )
        return ToolResult(content)


def skill_tools() -> list[Tool]:
    return [UseSkill(), SkillRead()]
