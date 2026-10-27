"""Tools that let agents discover and load skills on demand."""

from __future__ import annotations

from pydantic import BaseModel, Field

from argus.tools.base import Tool, ToolCallContext, ToolResult


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
            return ToolResult.error(f"No skill named '{args.name}'. Available: {available}")
        await ctx.run.sink.emit(
            "skill.loaded",
            {"skill_id": pkg.name, "name": pkg.name, "member_id": ctx.member_id, "version": pkg.version},
        )
        files = "\n".join(f"- {p}" for p in sorted(pkg.files)) or "(none)"
        tools = ", ".join(pkg.allowed_tools) or "any permitted tool"
        return ToolResult(
            f"# Skill: {pkg.name} (v{pkg.version})\n\n{pkg.body}\n\n"
            f"---\nBundled resources (read with skill_read):\n{files}\nTools this skill expects: {tools}",
            data={"skill": pkg.name},
        )


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
            return ToolResult.error(f"No skill named '{args.name}'")
        content = pkg.files.get(args.path.lstrip("./"))
        if content is None:
            return ToolResult.error(
                f"'{args.path}' not found in skill {pkg.name}. Files: {', '.join(sorted(pkg.files))}"
            )
        return ToolResult(content)


def skill_tools() -> list[Tool]:
    return [UseSkill(), SkillRead()]
