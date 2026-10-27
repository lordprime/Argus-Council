from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, func, select

from argus.api.deps import CurrentPrincipal, DbDep, write_scope
from argus.api.schemas import SkillIn, SkillPatch
from argus.core.errors import ArgusError, Conflict, Forbidden, NotFound
from argus.db.models import Council, Skill, SkillVersion
from argus.skills.catalog import marketplace_skills, tags_of
from argus.skills.library import builtin_skills, from_row, skill_permitted
from argus.skills.package import SkillError, SkillPackage, load_zip, parse_skill_md, validate_files

router = APIRouter(
    prefix="/skills", tags=["skills"], dependencies=[write_scope("skills:write", exempt=("/validate",))]
)

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


async def _used_by(db, owner_id, name: str) -> list[dict]:
    """Councils whose guardrails let their agents load ``name`` (skills allow-list minus deny-list)."""
    rows = await db.execute(select(Council).where(Council.owner_id == owner_id).order_by(Council.name))
    out = []
    for c in rows.scalars():
        policy = (c.guardrails or {}).get("skills") or {}
        allow, deny = policy.get("allow", ["*"]), policy.get("deny", [])
        if isinstance(allow, list) and isinstance(deny, list) and skill_permitted(name, allow, deny):
            out.append({"id": str(c.id), "name": c.name})
    return out


async def _latest(db, skill_id) -> SkillVersion | None:
    return (
        await db.execute(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _snapshot(db, row: Skill, principal, note: str, *, force: bool = False) -> SkillVersion | None:
    """Store an immutable version of ``row``; skipped when nothing changed since the latest one."""
    pkg = from_row(row)
    skill_md, files = pkg.render(), dict(pkg.files)
    latest = await _latest(db, row.id)
    if latest is not None and not force and latest.skill_md == skill_md and dict(latest.files) == files:
        return None
    ver = SkillVersion(
        skill_id=row.id,
        number=(latest.number if latest else 0) + 1,
        skill_md=skill_md,
        files=files,
        author_id=principal.id,
        author=principal.user.email,
        note=note[:200],
    )
    db.add(ver)
    await db.flush()
    return ver


async def _create(
    db, principal, pkg: SkillPackage, *, source: str, enabled: bool = True, note: str = "created"
) -> Skill:
    owner_id = principal.id
    if await _row(db, owner_id, pkg.name):
        raise Conflict(f"You already have a skill named '{pkg.name}'")
    row = Skill(owner_id=owner_id, name=pkg.name, source=source, enabled=enabled)
    _apply(row, pkg)
    db.add(row)
    await db.flush()
    await _snapshot(db, row, principal, note, force=True)
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
    row = await _create(db, principal, _parse(data), source="created", enabled=data.enabled)
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
    row = await _create(db, principal, pkg, source="upload", note="imported from archive")
    return _summary(from_row(row), source=row.source, shadows_builtin=row.name in builtin_skills())


# ------------------------------------------------------------------ marketplace
# Declared before "/{name}" so "marketplace" is never captured as a skill name.
@router.get("/marketplace")
async def list_marketplace(principal: CurrentPrincipal, db: DbDep) -> dict:
    mine = {
        r.name: r for r in (await db.execute(select(Skill).where(Skill.owner_id == principal.id))).scalars()
    }
    out = []
    for name, pkg in marketplace_skills().items():
        row = mine.get(name)
        out.append(
            {
                "name": name,
                "description": pkg.description,
                "version": pkg.version,
                "tags": tags_of(pkg),
                "allowed_tools": pkg.allowed_tools,
                "files": sorted(pkg.files),
                "installed": row is not None,
                "installed_version": row.version if row is not None else None,
                "installed_from_marketplace": row is not None and row.source == "marketplace",
            }
        )
    return {"skills": out}


@router.get("/marketplace/{name}")
async def get_marketplace_skill(name: str, _: CurrentPrincipal) -> dict:
    pkg = marketplace_skills().get(name)
    if pkg is None:
        raise NotFound("Marketplace skill not found")
    return {
        **_summary(pkg, source="marketplace"),
        "tags": tags_of(pkg),
        "skill_md": pkg.render(),
        "file_contents": pkg.files,
    }


@router.post("/marketplace/{name}/install", status_code=status.HTTP_201_CREATED)
async def install_marketplace_skill(name: str, principal: CurrentPrincipal, db: DbDep) -> dict:
    principal.require_scope("skills:write")
    src = marketplace_skills().get(name)
    if src is None:
        raise NotFound("Marketplace skill not found")
    # Round-trip through the same validation as user uploads, so the copy is held to identical rules.
    try:
        pkg = parse_skill_md(src.render())
        pkg.files = validate_files(dict(src.files))
    except SkillError as exc:
        raise InvalidSkill(str(exc)) from exc
    row = await _create(
        db, principal, pkg, source="marketplace", note=f"installed from marketplace (v{pkg.version})"
    )
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
    return {
        **_summary(pkg, **info),
        "skill_md": pkg.render(),
        "file_contents": pkg.files,
        "used_by_councils": await _used_by(db, principal.id, name),
    }


@router.get("/{name}/usage")
async def skill_usage(name: str, principal: CurrentPrincipal, db: DbDep) -> dict:
    if await _row(db, principal.id, name) is None and name not in builtin_skills():
        raise NotFound("Skill not found")
    return {"name": name, "used_by_councils": await _used_by(db, principal.id, name)}


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
    await _snapshot(db, row, principal, "updated")
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
    await db.execute(delete(SkillVersion).where(SkillVersion.skill_id == row.id))
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


# ------------------------------------------------------------------ versions
async def _owned(db, principal, name: str) -> Skill:
    row = await _row(db, principal.id, name)
    if row is None:
        raise (
            NotFound("Built-in skills have no version history")
            if name in builtin_skills()
            else NotFound("Skill not found")
        )
    return row


def _version_meta(v: SkillVersion) -> dict:
    try:
        semver = parse_skill_md(v.skill_md).version
    except SkillError:
        semver = None
    return {
        "number": v.number,
        "version": semver,
        "created_at": v.created_at.isoformat(),
        "author": v.author,
        "note": v.note,
        "files": sorted(v.files or {}),
    }


@router.get("/{name}/versions")
async def list_versions(name: str, principal: CurrentPrincipal, db: DbDep) -> dict:
    row = await _owned(db, principal, name)
    rows = (
        await db.execute(
            select(SkillVersion).where(SkillVersion.skill_id == row.id).order_by(SkillVersion.number.desc())
        )
    ).scalars()
    versions = [_version_meta(v) for v in rows]
    return {"name": name, "current": versions[0]["number"] if versions else None, "versions": versions}


async def _version(db, row: Skill, n: int) -> SkillVersion:
    v = (
        await db.execute(
            select(SkillVersion).where(SkillVersion.skill_id == row.id, SkillVersion.number == n)
        )
    ).scalar_one_or_none()
    if v is None:
        raise NotFound(f"Version {n} not found")
    return v


@router.get("/{name}/versions/{n}")
async def get_version(name: str, n: int, principal: CurrentPrincipal, db: DbDep) -> dict:
    v = await _version(db, await _owned(db, principal, name), n)
    return {**_version_meta(v), "name": name, "skill_md": v.skill_md, "file_contents": dict(v.files or {})}


@router.post("/{name}/versions/{n}/restore")
async def restore_version(name: str, n: int, principal: CurrentPrincipal, db: DbDep) -> dict:
    principal.require_scope("skills:write")
    row = await _owned(db, principal, name)
    v = await _version(db, row, n)
    try:
        pkg = parse_skill_md(v.skill_md)
        pkg.files = validate_files(dict(v.files or {}))
    except SkillError as exc:
        raise InvalidSkill(f"Version {n} no longer passes validation: {exc}") from exc
    if pkg.name != name:
        raise InvalidSkill("The stored version belongs to a different skill name")
    _apply(row, pkg)
    await db.flush()
    new = await _snapshot(db, row, principal, f"restored from v{n}", force=True)
    count = (
        await db.execute(
            select(func.count()).select_from(SkillVersion).where(SkillVersion.skill_id == row.id)
        )
    ).scalar_one()
    return {
        **_summary(from_row(row), source=row.source, enabled=row.enabled),
        "restored_from": n,
        "current_version": new.number if new else None,
        "version_count": count,
    }
