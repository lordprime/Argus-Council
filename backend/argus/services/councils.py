"""Council management: creation from rosters, membership edits and resizing."""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from argus.agents.roles import ROLES, roster_for
from argus.api.schemas import CouncilIn, CouncilPatch, MemberIn, MemberPatch
from argus.core.errors import Conflict, GuardrailViolation, NotFound
from argus.db.models import Council, CouncilMember


def _member_from_role(role: str, index: int, provider: str, model: str, taken: Counter[str]) -> CouncilMember:
    spec = ROLES[role]
    taken[role] += 1
    name = spec.title if taken[role] == 1 else f"{spec.title} {taken[role]}"
    return CouncilMember(
        name=name,
        role=role,
        provider=provider,
        model=model,
        system_prompt=spec.system_prompt,
        position=index,
        config={"tools": list(spec.default_tools), "overseer": spec.overseer},
    )


async def get_council(db: AsyncSession, owner_id: uuid.UUID, council_id: uuid.UUID) -> Council:
    council = (
        await db.execute(
            select(Council)
            .where(Council.id == council_id, Council.owner_id == owner_id)
            .options(selectinload(Council.members))
        )
    ).scalar_one_or_none()
    if council is None:
        raise NotFound("Council not found")
    return council


async def list_councils(db: AsyncSession, owner_id: uuid.UUID) -> list[Council]:
    rows = await db.execute(
        select(Council)
        .where(Council.owner_id == owner_id)
        .options(selectinload(Council.members))
        .order_by(Council.created_at.desc())
    )
    return list(rows.scalars())


async def _ensure_unique_name(
    db: AsyncSession, owner_id: uuid.UUID, name: str, exclude: uuid.UUID | None = None
):
    q = select(Council.id).where(Council.owner_id == owner_id, Council.name == name)
    if exclude:
        q = q.where(Council.id != exclude)
    if (await db.execute(q)).first():
        raise Conflict(f"A council named '{name}' already exists")


async def create_council(db: AsyncSession, owner_id: uuid.UUID, data: CouncilIn) -> Council:
    await _ensure_unique_name(db, owner_id, data.name)
    council = Council(
        owner_id=owner_id,
        name=data.name,
        description=data.description,
        mode=data.mode,
        min_size=data.min_size,
        max_size=data.max_size,
        guardrails=data.guardrails,
    )
    if data.members is not None:
        council.members = [CouncilMember(**m.model_dump(), position=i) for i, m in enumerate(data.members)]
    else:
        size = data.size or max(data.min_size, min(5, data.max_size))
        taken: Counter[str] = Counter()
        council.members = [
            _member_from_role(role, i, data.default_provider, data.default_model, taken)
            for i, role in enumerate(roster_for(data.mode, size))
        ]
    db.add(council)
    await db.flush()
    return await get_council(db, owner_id, council.id)


async def update_council(db: AsyncSession, council: Council, patch: CouncilPatch) -> Council:
    values = patch.model_dump(exclude_unset=True)
    if "name" in values and values["name"] != council.name:
        await _ensure_unique_name(db, council.owner_id, values["name"], exclude=council.id)
    lo = values.get("min_size", council.min_size)
    hi = values.get("max_size", council.max_size)
    if lo > hi:
        raise GuardrailViolation("min_size must be <= max_size")
    if not lo <= len(council.members) <= hi:
        raise GuardrailViolation(
            f"Current size {len(council.members)} would fall outside [{lo}, {hi}]; resize first"
        )
    for k, v in values.items():
        setattr(council, k, v)
    await db.flush()
    return council


def _renumber(council: Council) -> None:
    for i, m in enumerate(sorted(council.members, key=lambda m: m.position)):
        m.position = i


async def add_member(db: AsyncSession, council: Council, data: MemberIn) -> CouncilMember:
    if len(council.members) >= council.max_size:
        raise GuardrailViolation(f"Council is at max_size={council.max_size}")
    member = CouncilMember(**data.model_dump(), position=len(council.members))
    council.members.append(member)
    await db.flush()
    return member


def _find_member(council: Council, member_id: uuid.UUID) -> CouncilMember:
    for m in council.members:
        if m.id == member_id:
            return m
    raise NotFound("Member not found")


async def update_member(db: AsyncSession, council: Council, member_id: uuid.UUID, patch: MemberPatch):
    member = _find_member(council, member_id)
    values = patch.model_dump(exclude_unset=True)
    new_pos = values.pop("position", None)
    for k, v in values.items():
        setattr(member, k, v)
    if new_pos is not None:
        ordered = [m for m in sorted(council.members, key=lambda m: m.position) if m is not member]
        ordered.insert(min(new_pos, len(ordered)), member)
        for i, m in enumerate(ordered):
            m.position = i
    await db.flush()
    return member


async def remove_member(db: AsyncSession, council: Council, member_id: uuid.UUID) -> None:
    member = _find_member(council, member_id)
    if len(council.members) <= council.min_size:
        raise GuardrailViolation(f"Council is at min_size={council.min_size}")
    council.members.remove(member)
    _renumber(council)
    await db.flush()


async def resize_council(
    db: AsyncSession, council: Council, size: int, provider: str | None, model: str | None
) -> Council:
    """Grow from the mode's roster, or shrink by dropping the lowest-priority seats."""
    if not council.min_size <= size <= council.max_size:
        raise GuardrailViolation(f"size {size} outside [{council.min_size}, {council.max_size}]")
    members = sorted(council.members, key=lambda m: m.position)
    current = len(members)
    if size < current:
        for m in members[size:]:
            council.members.remove(m)
    elif size > current:
        ref = members[0] if members else None
        prov = provider or (ref.provider if ref else "mock")
        mdl = model or (ref.model if ref else "mock-1")
        wanted = roster_for(council.mode, size)
        deficit = Counter(wanted) - Counter(m.role for m in members)
        additions: list[str] = []
        for role in wanted:
            if deficit[role] > 0:
                additions.append(role)
                deficit[role] -= 1
        # Custom rosters may already cover the defaults; pad with extra workers.
        extra = roster_for(council.mode, size + current)[size:]
        additions = (additions + extra)[: size - current]
        taken = Counter(m.role for m in members)
        for i, role in enumerate(additions):
            council.members.append(_member_from_role(role, current + i, prov, mdl, taken))
    _renumber(council)
    await db.flush()
    return council
