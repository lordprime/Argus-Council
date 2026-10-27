"""Skill packages: a ``SKILL.md`` (YAML frontmatter + instructions) plus bundled text resources.

Mirrors Claude Code skills. Agents always see each skill's *name and
description*; the full instructions and resources load only when an agent
calls ``use_skill`` (progressive disclosure keeps context small).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
MAX_DESCRIPTION = 1024
MAX_BODY = 200_000
MAX_FILE = 512 * 1024
MAX_FILES = 64
MAX_TOTAL = 4 * 1024 * 1024
_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".tex",
    ".bib",
    ".csv",
    ".html",
    ".css",
    ".sh",
    ".sql",
    ".cfg",
    ".ini",
    ".rst",
    ".jinja",
    ".j2",
    ".xml",
    ".svg",
}


class SkillError(ValueError):
    pass


@dataclass
class SkillPackage:
    name: str
    description: str
    body: str
    version: str = "1.0.0"
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # relative path -> text content

    @property
    def frontmatter(self) -> dict[str, Any]:
        fm: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.version:
            fm["version"] = self.version
        if self.allowed_tools:
            fm["allowed-tools"] = self.allowed_tools
        if self.metadata:
            fm["metadata"] = self.metadata
        return fm

    def render(self) -> str:
        head = yaml.safe_dump(self.frontmatter, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{head}\n---\n\n{self.body.strip()}\n"


def parse_skill_md(text: str) -> SkillPackage:
    if len(text) > MAX_BODY:
        raise SkillError("SKILL.md is too large")
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*(?:\n|$)(.*)$", text, re.S)
    if not m:
        raise SkillError("SKILL.md must start with YAML frontmatter delimited by '---'")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"Invalid YAML frontmatter: {exc}") from exc
    if not isinstance(fm, dict):
        raise SkillError("Frontmatter must be a mapping")
    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    if not NAME_RE.match(name):
        raise SkillError("name must be 1-64 chars of lowercase letters, digits and hyphens")
    if not description:
        raise SkillError("description is required (it tells agents when to use the skill)")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"description must be at most {MAX_DESCRIPTION} characters")
    tools = fm.get("allowed-tools") or fm.get("allowed_tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise SkillError("allowed-tools must be a list of tool names")
    body = m.group(2).strip()
    if not body:
        raise SkillError("SKILL.md needs instructions after the frontmatter")
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    return SkillPackage(
        name=name,
        description=description,
        body=body,
        version=str(fm.get("version") or "1.0.0"),
        allowed_tools=tools,
        metadata=meta,
    )


def _safe_relpath(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts or any(part.startswith(".") for part in p.parts):
        raise SkillError(f"Unsafe or hidden path in skill: {path}")
    return str(p)


def validate_files(files: dict[str, str]) -> dict[str, str]:
    if len(files) > MAX_FILES:
        raise SkillError(f"A skill may bundle at most {MAX_FILES} files")
    out: dict[str, str] = {}
    total = 0
    for path, content in files.items():
        rel = _safe_relpath(path)
        if rel == "SKILL.md":
            continue
        if PurePosixPath(rel).suffix.lower() not in _TEXT_SUFFIXES:
            raise SkillError(f"Unsupported file type in skill: {rel}")
        size = len(content.encode())
        if size > MAX_FILE:
            raise SkillError(f"{rel} exceeds {MAX_FILE // 1024} KiB")
        total += size
        out[rel] = content
    if total > MAX_TOTAL:
        raise SkillError("Skill bundle is too large")
    return out


def load_zip(data: bytes) -> SkillPackage:
    """Load a skill from a zip archive (SKILL.md at the root or inside one top-level folder)."""
    if len(data) > MAX_TOTAL * 2:
        raise SkillError("Archive is too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillError("Not a valid zip archive") from exc
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES + 1:
        raise SkillError("Too many files in archive")
    if sum(i.file_size for i in infos) > MAX_TOTAL:  # guards against zip bombs before extraction
        raise SkillError("Archive expands beyond the size limit")
    names = [i.filename for i in infos]
    skill_md = next(
        (n for n in names if PurePosixPath(n).name == "SKILL.md" and len(PurePosixPath(n).parts) <= 2), None
    )
    if skill_md is None:
        raise SkillError("Archive must contain SKILL.md")
    prefix = str(PurePosixPath(skill_md).parent)
    prefix = "" if prefix == "." else prefix + "/"
    files: dict[str, str] = {}
    pkg: SkillPackage | None = None
    for info in infos:
        if info.filename.startswith("__MACOSX/"):
            continue
        if prefix and not info.filename.startswith(prefix):
            raise SkillError("All files must live under the skill folder")
        rel = info.filename[len(prefix) :]
        raw = zf.read(info)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError(f"{rel} is not UTF-8 text") from exc
        if rel == "SKILL.md":
            pkg = parse_skill_md(text)
        else:
            files[rel] = text
    assert pkg is not None
    pkg.files = validate_files(files)
    return pkg


def load_dir(root: Path) -> SkillPackage:
    pkg = parse_skill_md((root / "SKILL.md").read_text(encoding="utf-8"))
    files = {
        str(p.relative_to(root).as_posix()): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and p.name != "SKILL.md"
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    }
    pkg.files = validate_files(files)
    return pkg
