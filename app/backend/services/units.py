"""Units of the organisation (corps, brigades: scope groups of a unit kind): what a post or an observation belongs to.

The units of a post come from the scope tree every request loads (ScopeTree.units_of_post); the units of an
observation are the observation_units rows written at its create, from the database (current_units_of_group).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, delete, false
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node, ScopeTree
from app.backend.enums.general import Permissions, ScopeGroupKind, UnitPlacement
from app.backend.models.observation import Observation
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


async def current_units_of_group(session: AsyncSession, group_id: Optional[int]) -> Dict[str, int]:
    """The units a group is in or above, as the database holds them now (unit kind -> group id): what
    ScopeTree.units_of_group gives for the same state, read with plain selects up the parent_id chain (archived
    groups count; a plain group is skipped; a group seen twice or a missing one ends the chain). For the writes that
    record units: the tree of the request may be older than the lock the write holds."""
    chain: List[Tuple[int, str]] = []
    seen = set()
    while group_id is not None and group_id not in seen:
        seen.add(group_id)
        row = (await session.exec(select(ScopeGroup.parent_id, ScopeGroup.kind).where(ScopeGroup.id == group_id))).first()
        if row is None:
            break
        chain.append((group_id, row[1]))
        group_id = row[0]
    return {kind: unit_id for unit_id, kind in reversed(chain) if kind != ScopeGroupKind.GROUP.value}


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


def unit_counts(auth: AuthContext, unit_id: int, viewable: Optional[FrozenSet[int]]) -> Tuple[int, int]:
    """(active units directly inside it the caller sees (scope_group.view), active posts at any depth below it with
    post.view in effect): the brigade_count and post_count of a unit answer, from the request's tree. viewable is
    auth.posts_with(Permissions.POST_VIEW) (None: every post), read once per response by the caller"""
    tree = auth.tree
    children = sum(
        1 for group_id, parent_id in tree.group_parent.items()
        if parent_id == unit_id and tree.group_kind.get(group_id) not in (None, ScopeGroupKind.GROUP.value)
        and group_id not in tree.archived_groups and auth.covers(Permissions.SCOPE_GROUP_VIEW, Node.group(group_id))
    )
    posts = sum(
        1 for post_id in tree.posts_under(Node.group(unit_id))
        if post_id not in tree.archived_posts and (viewable is None or post_id in viewable)
    )
    return children, posts


def posts_within(tree: ScopeTree, kind: ScopeGroupKind, unit_id: int, params) -> FrozenSet[int]:
    """The posts of a unit's posts list (GET /corps/{id}/posts): under the unit, narrowed by the unit fields of the
    query model (a corps_id or brigade_id elsewhere gives nothing)"""
    under = unit_post_ids(tree, kind, unit_id)
    asked = unit_filter_of(tree, params)
    return under if asked is None else under & asked


def observation_unit_rows(units: Mapping[str, int], observation_id: int, created_at: datetime) -> List[ObservationUnit]:
    """The observation_units rows of a new observation: one per unit above its post (current_units_of_group of the
    post's group, read after the post lock)"""
    return [ObservationUnit(observation_id=observation_id, group_id=unit_id, created_at=created_at)
            for unit_id in units.values()]


async def recorded_units(session: AsyncSession, observation_ids: Iterable[int]) -> Dict[int, Dict[str, Tuple[int, str]]]:
    """observation id -> unit kind -> (group id, today's name) of the units recorded at its create (one query)"""
    ids = sorted(set(observation_ids))
    if not ids:
        return {}
    rows = await session.exec(
        select(ObservationUnit.observation_id, ScopeGroup.kind, ScopeGroup.id, ScopeGroup.name)
        .join(ScopeGroup, ScopeGroup.id == ObservationUnit.group_id)
        .where(ObservationUnit.observation_id.in_(ids))
    )
    units: Dict[int, Dict[str, Tuple[int, str]]] = {}
    for observation_id, kind, group_id, name in rows.all():
        units.setdefault(observation_id, {})[kind] = (group_id, name)
    return units


def observation_unit_condition(tree: ScopeTree, kind: ScopeGroupKind, unit_id: Optional[int],
                               placement: UnitPlacement) -> Optional[ColumnElement]:
    """Observations of a unit (None without a unit). recorded: the unit stored at the observation's create
    (observation_units); current: its post is under the unit now and not archived (a post that left the unit, or was
    archived, no longer counts there). An id of another kind or unknown: no observation. The caller's access is a
    separate condition: this one narrows, never widens."""
    if unit_id is None:
        return None
    return observation_units_any(tree, {kind: [unit_id]}, placement)


def _units_of_kind(tree: ScopeTree, kind: ScopeGroupKind, unit_ids: Iterable[int]) -> List[int]:
    return sorted({unit_id for unit_id in unit_ids if tree.group_kind.get(unit_id) == ScopeGroupKind(kind).value})


def recorded_in(units: List[int], created_from: Optional[datetime] = None,
                created_to: Optional[datetime] = None) -> ColumnElement:
    """Observations recorded in any of these units; with a period, only those created in it (the subquery then reads
    a range of the primary key (group_id, created_at, observation_id) of observation_units)"""
    rows = select(ObservationUnit.observation_id).where(ObservationUnit.group_id.in_(units))
    if created_from is not None:
        rows = rows.where(ObservationUnit.created_at >= created_from)
    if created_to is not None:
        rows = rows.where(ObservationUnit.created_at <= created_to)
    return Observation.id.in_(rows)


def observation_units_filter(tree: ScopeTree, params) -> Optional[ColumnElement]:
    """observation_unit_condition of the <kind>_id fields and placement of a query model (all must hold)"""
    placement = getattr(params, "placement", UnitPlacement.RECORDED)
    conditions = [condition for kind in UNIT_KINDS
                  if (condition := observation_unit_condition(
                      tree, kind, getattr(params, f"{kind.value}_id", None), placement)) is not None]
    return and_(*conditions) if conditions else None


def observation_units_any(tree: ScopeTree, unit_ids: Mapping[ScopeGroupKind, Optional[Iterable[int]]],
                          placement: UnitPlacement) -> Optional[ColumnElement]:
    """Observations of any of the units of a kind (corps_ids: one of these corps), every kind given holding (and
    one of these brigades); None when no unit is asked for"""
    per_kind = []
    for kind, ids in unit_ids.items():
        if not ids:
            continue
        units = _units_of_kind(tree, kind, ids)     # an id of another kind or unknown matches nothing
        if not units:
            per_kind.append(false())
        elif UnitPlacement(placement) is UnitPlacement.RECORDED:
            # One subquery for every unit of the kind: an IN in the top-level AND, a semi-join on MariaDB
            per_kind.append(recorded_in(units))
        else:
            posts = frozenset().union(*(unit_post_ids(tree, kind, unit_id) for unit_id in units))
            per_kind.append(Observation.post_id.in_(sorted(posts - tree.archived_posts)))
    return and_(*per_kind) if per_kind else None


def visible_unit_ids(auth: AuthContext, unit_ids: Mapping[ScopeGroupKind, Optional[Iterable[int]]],
                     permissions: Sequence[Permissions]) -> List[int]:
    """The asked ids a report may name: of the right kind and inside where the caller holds scope_group.view or every
    one of the permissions (a report needs observation.view and export.create: both) (any other id is shown as its
    number only)"""
    return sorted(unit_id for kind, ids in unit_ids.items() for unit_id in _units_of_kind(auth.tree, kind, ids or ())
                  if auth.covers(Permissions.SCOPE_GROUP_VIEW, Node.group(unit_id))
                  or all(auth.covers(permission, Node.group(unit_id)) for permission in permissions))


@dataclass(frozen=True)
class UnitLabel:
    """A unit as a report shows it: today's name, archived today or not"""
    id: int
    name: str
    archived: bool


async def unit_labels(session: AsyncSession, group_ids: Iterable[int]) -> Dict[int, UnitLabel]:
    """group id -> its label (one query); an unknown id is left out"""
    ids = sorted(set(group_ids))
    if not ids:
        return {}
    rows = await session.exec(select(ScopeGroup.id, ScopeGroup.name, ScopeGroup.is_deleted).where(ScopeGroup.id.in_(ids)))
    return {group_id: UnitLabel(group_id, name, bool(is_deleted)) for group_id, name, is_deleted in rows.all()}


async def observation_unit_labels(session: AsyncSession, tree: ScopeTree, observations: Iterable[Observation],
                                  placement: UnitPlacement) -> Dict[int, Dict[str, UnitLabel]]:
    """observation id -> unit kind -> label: recorded, the units stored at its create; current, the units its post
    is in now (the observations come with their post loaded)"""
    observations = list(observations)
    units: Dict[int, Dict[str, int]] = {}
    if UnitPlacement(placement) is UnitPlacement.RECORDED:
        ids = sorted({observation.id for observation in observations})
        if ids:
            rows = await session.exec(
                select(ObservationUnit.observation_id, ScopeGroup.kind, ScopeGroup.id)
                .join(ScopeGroup, ScopeGroup.id == ObservationUnit.group_id)
                .where(ObservationUnit.observation_id.in_(ids))
            )
            for observation_id, kind, group_id in rows.all():
                units.setdefault(observation_id, {})[kind] = group_id
    else:
        for observation in observations:
            group_id = observation.post.group_id if observation.post is not None else None
            units[observation.id] = post_units(tree, group_id)
    labels = await unit_labels(session, (group_id for found in units.values() for group_id in found.values()))
    return {observation_id: {kind: labels[group_id] for kind, group_id in found.items() if group_id in labels}
            for observation_id, found in units.items()}
