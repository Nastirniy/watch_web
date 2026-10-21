"""Unit structure at start-up: corps (directly under the root) > brigade (inside a corps) > post. No FastAPI here.

These are the unit rules of check_placement (services/scope_groups.py), read over the whole table, archived rows
included (a restore would bring a misplaced unit back). Plain groups are not checked.
"""
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy.orm import aliased
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.enums.general import ScopeGroupKind
from app.backend.models.scope_group import ScopeGroup


@dataclass(frozen=True)
class MisplacedUnit:
    group_id: int
    kind: str
    parent_id: Optional[int]
    parent_kind: Optional[str]      # None: directly under the root


async def misplaced_units(session: AsyncSession) -> List[MisplacedUnit]:
    """Corps with a parent and brigades whose parent is not a corps, by id"""
    parent = aliased(ScopeGroup)
    rows = (await session.exec(
        select(ScopeGroup.id, ScopeGroup.kind, ScopeGroup.parent_id, parent.kind)
        .join(parent, parent.id == ScopeGroup.parent_id, isouter=True)
        .where(ScopeGroup.kind.in_([ScopeGroupKind.CORPS.value, ScopeGroupKind.BRIGADE.value]))
        .order_by(ScopeGroup.id)
    )).all()
    corps, brigade = ScopeGroupKind.CORPS.value, ScopeGroupKind.BRIGADE.value
    return [
        MisplacedUnit(group_id, kind, parent_id, parent_kind)
        for group_id, kind, parent_id, parent_kind in rows
        if (kind == corps and parent_id is not None) or (kind == brigade and parent_kind != corps)
    ]


async def check_unit_structure(session: AsyncSession) -> None:
    """Stop the start when a corps or a brigade sits where no API route puts one"""
    found = await misplaced_units(session)
    if not found:
        return
    listing = ", ".join(
        f"{m.kind} id {m.group_id} (parent {'the root' if m.parent_id is None else f'{m.parent_kind} id {m.parent_id}'})"
        for m in found)
    raise RuntimeError(
        f"unit structure is broken: {listing}. A corps sits directly under the root and a brigade inside a corps; "
        "no API route creates such a row. Fix the rows and start again, for example move a brigade into a corps with "
        "UPDATE scope_groups SET parent_id = <corps id>, parent_key = <corps id> WHERE id = <brigade id> (see DEPLOYMENT.md)")
