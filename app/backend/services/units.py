"""Units of the organisation (corps, brigades: scope groups of a unit kind): what a post or an observation belongs to.

The units of a post come from the scope tree every request loads (ScopeTree.units_of_post); the units of an
observation are the observation_units rows written at its create.
"""
from typing import Dict, FrozenSet, Iterable, Mapping, Optional

from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node, ScopeTree
from app.backend.enums.general import ScopeGroupKind
from app.backend.models.observation_unit import ObservationUnit
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.scope_groups import UNIT_KINDS


async def delete_observation_units(session: AsyncSession, observation_ids: Iterable[int]) -> int:
    """Remove the unit rows of these observations (a physical removal of observations, in its transaction; no route
    removes observations today). Returns the number of rows removed; the caller commits."""
    ids = sorted(set(observation_ids))
    if not ids:
        return 0
    result = await session.exec(delete(ObservationUnit).where(ObservationUnit.observation_id.in_(ids)))
    return result.rowcount


def post_units(tree: ScopeTree, group_id: Optional[int]) -> Dict[str, int]:
    """The units a post in this group sits in now (unit kind -> group id); {} directly under the root. Read through
    the post's group, not the post: a post created or moved in this request is right with the tree it loaded."""
    return tree.units_of_group(group_id) if group_id is not None else {}


async def unit_names(session: AsyncSession, group_ids: Iterable[int]) -> Dict[int, str]:
    """group id -> its name today (one query)"""
    ids = sorted(set(group_ids))
    if not ids:
        return {}
    rows = await session.exec(select(ScopeGroup.id, ScopeGroup.name).where(ScopeGroup.id.in_(ids)))
    return {group_id: name for group_id, name in rows.all()}


async def post_unit_names(session: AsyncSession, tree: ScopeTree, group_ids: Iterable[Optional[int]]) -> Dict[int, str]:
    """The names of every unit above posts in these groups (one query for a page of posts)"""
    return await unit_names(session, (unit_id for group_id in set(group_ids)
                                      for unit_id in post_units(tree, group_id).values()))


def unit_post_ids(tree: ScopeTree, kind: ScopeGroupKind, unit_id: Optional[int]) -> Optional[FrozenSet[int]]:
    """The posts under the unit now (archived ones included; the list's own archive rules apply), None without a
    unit; an id of another kind, a plain group or an unknown id: no post"""
    if unit_id is None:
        return None
    if tree.group_kind.get(unit_id) != ScopeGroupKind(kind).value:
        return frozenset()
    return tree.posts_under(Node.group(unit_id))


def units_filter(tree: ScopeTree, units: Mapping[ScopeGroupKind, Optional[int]]) -> Optional[FrozenSet[int]]:
    """The posts in every unit asked for (corps_id and brigade_id: both must hold), None when none is asked for. A
    restriction of its own next to the caller's access: it narrows, never widens."""
    posts: Optional[FrozenSet[int]] = None
    for kind, unit_id in units.items():
        under = unit_post_ids(tree, kind, unit_id)
        if under is not None:
            posts = under if posts is None else posts & under
    return posts


def unit_filter_of(tree: ScopeTree, params) -> Optional[FrozenSet[int]]:
    """units_filter of the <kind>_id fields of a query model (corps_id, brigade_id: one per UNIT_KINDS)"""
    return units_filter(tree, {kind: getattr(params, f"{kind.value}_id", None) for kind in UNIT_KINDS})
