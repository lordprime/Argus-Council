"""Curated skill marketplace: an in-repo catalogue under ``argus/skills/marketplace/<name>/``.

The catalogue ships inside the ``argus`` package (the API image's build context is ``backend/``
and only ``argus/`` is copied in), so the index is built from disk at runtime with no network.
Each entry is an ordinary skill package and is validated with the same rules as uploads.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from argus.core.logging import get_logger
from argus.skills.package import SkillError, SkillPackage, load_dir

log = get_logger(__name__)
MARKETPLACE_DIR = Path(__file__).parent / "marketplace"


@lru_cache
def marketplace_skills() -> dict[str, SkillPackage]:
    out: dict[str, SkillPackage] = {}
    root = MARKETPLACE_DIR
    for d in sorted(root.iterdir()) if root.exists() else []:
        if not (d / "SKILL.md").is_file():
            continue
        try:
            pkg = load_dir(d)
        except SkillError as exc:
            log.error("skills.marketplace_invalid", skill=d.name, error=str(exc))
            continue
        if pkg.name != d.name:
            log.error("skills.marketplace_name_mismatch", skill=d.name, name=pkg.name)
            continue
        out[pkg.name] = pkg
    return out


def tags_of(pkg: SkillPackage) -> list[str]:
    tags = pkg.metadata.get("tags") if isinstance(pkg.metadata, dict) else None
    return [str(t) for t in tags] if isinstance(tags, list) else []
