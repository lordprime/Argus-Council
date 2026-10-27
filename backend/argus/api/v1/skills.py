from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep
from argus.api.schemas import SkillIn, SkillPatch
from argus.core.errors import ArgusError, Conflict, Forbidden, NotFound
from argus.db.models import Skill
from argus.skills.library import builtin_skills, from_row
from argus.skills.package import SkillError, SkillPackage, load_zip, parse_skill_md, validate_files

router = APIRouter(prefix="/skills", tags=["skills"])

MAX_UPLOAD = 8 * 1024 * 1024


class InvalidSkill(ArgusError):
    code = "invalid_skill"


def _summary(pkg: SkillPackage, *, source: str, enabled: bool = True, shadows_builtin: bool = False) -> dict:
    return {
        "name": pkg.name,
        "description": pkg.description,
        "version": pkg.version,
        "allowed_tools": pkg.allowed_tools,
        "files": sorted(pkg.files),
        "source": source,
        "enabled": enabled,
        "builtin": source == "builtin",
        "shadows_builtin": shadows_builtin,
    }


def _parse(data: SkillIn) -> SkillPackage:
    try:
        pkg = parse_skill_md(data.skill_md)
        pkg.files = validate_files(data.files)
    except SkillError as exc:
        raise InvalidSkill(str(exc)) from exc
    return pkg


async def _row(db, owner_id, name: str) -> Skill | None:
    return (
        await db.execute(select(Skill).where(Skill.owner_id == owner_id, Skill.name == name))
    ).scalar_one_or_none()


def _apply(row: Skill, pkg: SkillPackage) -> None:
    row.description = pkg.description
    row.version = pkg.version
    row.body = pkg.body
    row.allowed_tools = pkg.allowed_tools
    row.meta = pkg.metadata
    row.files = pkg.files


async def _create(db, owner_id, pkg: SkillPackage, *, source: str, enabled: bool = True) -> Skill:
    if await _row(db, owner_id, pkg.name):
        raise Conflict(f"You already have a skill named '{pkg.name}'")
    row = Skill(owner_id=owner_id, name=pkg.name, source=source, enabled=enabled)
    _apply(row, pkg)
    db.add(row)
    await db.flush()
    return row


@router.get("")
async def list_skills(principal: CurrentPrincipal, db: DbDep) -> dict:
    rows = {
        r.name: r for r in (await db.execute(select(Skill).where(Skill.owner_id == principal.id))).scalars()
    }
    builtins = builtin_skills()
    out = [
        _summary(from_row(r), source=r.source, enabled=r.enabled, shadows_builtin=r.name in builtins)
        for r in rows.values()
    ]
    out += [_summary(p, source="builtin") for n, p in builtins.items() if n not in rows]
    return {"skills": sorted(out, key=lambda s: s["name"])}


@router.post("/validate")
async def validate(data: SkillIn, _: CurrentPrincipal) -> dict:
    try:
        pkg = parse_skill_md(data.skill_md)
        pkg.files = validate_files(data.files)
    except SkillError as exc:
        return {"valid": False, "errors": [str(exc)]}
    return {"valid": True, "errors": [], "skill": _summary(pkg, source="created")}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(data: SkillIn, principal: CurrentPrincipal, db: DbDep) -> dict:
    principal.require_scope("skills:write")
    row = await _create(db, principal.id, _parse(data), source="created", enabled=data.enabled)
    return _summary(
        from_row(row), source=row.source, enabled=row.enabled, shadows_builtin=row.name in builtin_skills()
    )


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_skill(principal: CurrentPrincipal, db: DbDep, file: UploadFile = File(...)) -> dict:
    principal.require_scope("skills:write")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise InvalidSkill("Archive too large")
    try:
        pkg = load_zip(data)
    except SkillError as exc:
        raise InvalidSkill(str(exc)) from exc
    row = await _create(db, principal.id, pkg, source="upload")
    return _summary(from_row(row), source=row.source, shadows_builtin=row.name in builtin_skills())


@router.get("/{name}")
async def get_skill(name: str, principal: CurrentPrincipal, db: DbDep) -> dict:
    row = await _row(db, principal.id, name)
    if row is not None:
        pkg, info = from_row(row), {"source": row.source, "enabled": row.enabled}
    elif name in builtin_skills():
        pkg, info = builtin_skills()[name], {"source": "builtin", "enabled": True}
    else:
        raise NotFound("Skill not found")
    return {**_summary(pkg, **info), "skill_md": pkg.render(), "file_contents": pkg.files}


@router.put("/{name}")
async def update_skill(name: str, data: SkillIn, principal: CurrentPrincipal, db: DbDep) -> dict:
    principal.require_scope("skills:write")
    pkg = _parse(data)
    if pkg.name != name:
        raise InvalidSkill("The name in SKILL.md must match the skill being updated")
    row = await _row(db, principal.id, name)
    if row is None:
        if name in builtin_skills():
            raise Forbidden(
                "Built-in skills are read-only; create your own copy with the same name to override it"
            )
        raise NotFound("Skill not found")
    _apply(row, pkg)
    row.enabled = data.enabled
    await db.flush()
    return _summary(from_row(row), source=row.source, enabled=row.enabled)


@router.patch("/{name}")
async def toggle_skill(name: str, data: SkillPatch, principal: CurrentPrincipal, db: DbDep) -> dict:
    row = await _row(db, principal.id, name)
    if row is None:
        raise NotFound("Skill not found")
    row.enabled = data.enabled
    return {"name": name, "enabled": row.enabled}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(name: str, principal: CurrentPrincipal, db: DbDep):
    principal.require_scope("skills:write")
    row = await _row(db, principal.id, name)
    if row is None:
        raise (
            Forbidden("Built-in skills cannot be deleted")
            if name in builtin_skills()
            else NotFound("Skill not found")
        )
    await db.delete(row)


@router.get("/{name}/export")
async def export_skill(name: str, principal: CurrentPrincipal, db: DbDep) -> Response:
    row = await _row(db, principal.id, name)
    pkg = from_row(row) if row else builtin_skills().get(name)
    if pkg is None:
        raise NotFound("Skill not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{pkg.name}/SKILL.md", pkg.render())
        for path, content in sorted(pkg.files.items()):
            zf.writestr(f"{pkg.name}/{path}", content)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pkg.name}.zip"'},
    )
