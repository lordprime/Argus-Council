"""Skill library: built-in skills shipped with Argus plus each user's own skills."""

from __future__ import annotations

import fnmatch
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.core.logging import get_logger
from argus.db.models import Skill
from argus.skills.package import SkillError, SkillPackage, load_dir

log = get_logger(__name__)
BUILTIN_DIR = Path(__file__).parent / "builtin"


@lru_cache
def builtin_skills() -> dict[str, SkillPackage]:
    out: dict[str, SkillPackage] = {}
    for d in sorted(BUILTIN_DIR.iterdir()) if BUILTIN_DIR.exists() else []:
        if (d / "SKILL.md").is_file():
            try:
                pkg = load_dir(d)
            except SkillError as exc:
                log.error("skills.builtin_invalid", skill=d.name, error=str(exc))
                continue
            pkg.metadata = {**pkg.metadata, "builtin": True}
            out[pkg.name] = pkg
    return out


def from_row(row: Skill) -> SkillPackage:
    return SkillPackage(
        name=row.name,
        description=row.description,
        body=row.body,
        version=row.version,
        allowed_tools=list(row.allowed_tools or []),
        metadata=dict(row.meta or {}),
        files=dict(row.files or {}),
    )


def skill_permitted(name: str, allow: list[str], deny: list[str]) -> bool:
    if any(fnmatch.fnmatchcase(name, p) for p in deny):
        return False
    return any(fnmatch.fnmatchcase(name, p) for p in allow)


async def skills_for_owner(sessionmaker: async_sessionmaker, owner_id: uuid.UUID) -> dict[str, SkillPackage]:
    """Built-ins overlaid by the owner's enabled skills (a user skill shadows a built-in of the same name)."""
    skills = dict(builtin_skills())
    async with sessionmaker() as s:
        rows = await s.execute(select(Skill).where(Skill.owner_id == owner_id, Skill.enabled.is_(True)))
        for row in rows.scalars():
            skills[row.name] = from_row(row)
    return skills
