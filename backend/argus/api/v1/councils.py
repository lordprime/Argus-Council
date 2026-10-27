from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from argus.agents.roles import DEFAULT_ROSTERS, ROLES
from argus.api.deps import CurrentPrincipal, DbDep, write_scope
from argus.api.schemas import CouncilIn, CouncilOut, CouncilPatch, MemberIn, MemberOut, MemberPatch, ResizeIn
from argus.services import councils as svc

router = APIRouter(tags=["councils"], dependencies=[write_scope("councils:write")])


@router.get("/roles")
async def list_roles(_: CurrentPrincipal) -> dict:
    return {
        "roles": [
            {
                "key": r.key,
                "title": r.title,
                "description": r.description,
                "overseer": r.overseer,
                "default_tools": list(r.default_tools),
                # Used when a seat's own prompt is left blank.
                "default_system_prompt": r.system_prompt,
            }
            for r in ROLES.values()
        ],
        "rosters": DEFAULT_ROSTERS,
    }


@router.get("/councils", response_model=list[CouncilOut])
async def list_councils(principal: CurrentPrincipal, db: DbDep):
    return await svc.list_councils(db, principal.id)


@router.post("/councils", response_model=CouncilOut, status_code=status.HTTP_201_CREATED)
async def create_council(data: CouncilIn, principal: CurrentPrincipal, db: DbDep):
    return await svc.create_council(db, principal.id, data)


@router.get("/councils/{council_id}", response_model=CouncilOut)
async def get_council(council_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    return await svc.get_council(db, principal.id, council_id)


@router.patch("/councils/{council_id}", response_model=CouncilOut)
async def update_council(council_id: uuid.UUID, data: CouncilPatch, principal: CurrentPrincipal, db: DbDep):
    council = await svc.get_council(db, principal.id, council_id)
    return await svc.update_council(db, council, data)


@router.delete("/councils/{council_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_council(council_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    council = await svc.get_council(db, principal.id, council_id)
    await db.delete(council)


@router.post("/councils/{council_id}/resize", response_model=CouncilOut)
async def resize_council(council_id: uuid.UUID, data: ResizeIn, principal: CurrentPrincipal, db: DbDep):
    council = await svc.get_council(db, principal.id, council_id)
    return await svc.resize_council(db, council, data.size, data.provider, data.model)


@router.post("/councils/{council_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(council_id: uuid.UUID, data: MemberIn, principal: CurrentPrincipal, db: DbDep):
    council = await svc.get_council(db, principal.id, council_id)
    return await svc.add_member(db, council, data)


@router.patch("/councils/{council_id}/members/{member_id}", response_model=MemberOut)
async def update_member(
    council_id: uuid.UUID, member_id: uuid.UUID, data: MemberPatch, principal: CurrentPrincipal, db: DbDep
):
    council = await svc.get_council(db, principal.id, council_id)
    return await svc.update_member(db, council, member_id, data)


@router.delete("/councils/{council_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(council_id: uuid.UUID, member_id: uuid.UUID, principal: CurrentPrincipal, db: DbDep):
    council = await svc.get_council(db, principal.id, council_id)
    await svc.remove_member(db, council, member_id)
