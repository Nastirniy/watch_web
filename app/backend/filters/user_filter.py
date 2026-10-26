"""
UserFilterService - Filter service for User entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, Optional, Iterable
from sqlalchemy import false, or_
from sqlalchemy.sql.selectable import Select
from sqlmodel import select

from app.backend.enums.general import UserType
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.user import User
from app.backend.auth.presence import online_user_ids_select
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine
from app.backend.services.units import UnitNodes


@dataclass(frozen=True, kw_only=True)
class UserCriteria(ListCriteria):
    """What UserFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    username: Optional[str] = None
    username_contains: Optional[str] = None
    username_alias: Optional[str] = None
    username_alias_contains: Optional[str] = None
    post_id: Optional[int] = None
    corps_id: Optional[int] = None      # the route turns post_id / corps_id / brigade_id into UnitNodes (services/units.py membership_nodes)
    brigade_id: Optional[int] = None
    membership: str = "any"             # any | home | assigned: how the unit fields match
    user_type: Optional[UserType] = None
    is_active: Optional[bool] = None
    is_online: Optional[bool] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None
    last_login_from: Optional[datetime] = None
    last_login_to: Optional[datetime] = None
    include_deleted: bool = False


class UserFilterService(SimpleFilterEngine):
    """Filter service for User entity"""

    MODEL = User
    CRITERIA = UserCriteria
    SORTABLE_FIELDS = ["id", "username", "username_alias", "created_at", "updated_at", "last_login_at", "is_active", "last_activity_at"]

    @classmethod
    def _apply_scope(
        cls,
        query: Select,
        params: UserCriteria,
        access_post_ids: Optional[Iterable[int]],
        archived_post_ids: Optional[FrozenSet[int]],
        caller_id: Optional[int],
    ) -> Select:
        """Archived users only on include_deleted and only where user.archive is in effect (by home post);
        access_post_ids limits to users whose home post is in it (None: every user, users without a home post
        included), caller_id is listed whatever its home post"""
        query = cls._apply_soft_delete(query, params, User.post_id, archived_post_ids)
        if access_post_ids is not None:
            in_scope = User.post_id.in_(list(access_post_ids))
            query = query.where(or_(in_scope, User.id == caller_id) if caller_id is not None else in_scope)
        return query

    @classmethod
    def _apply_membership(cls, query: Select, params: UserCriteria, unit_nodes: Optional[UnitNodes],
                          assigned_nodes: Optional[UnitNodes]) -> Select:
        """post_id / corps_id / brigade_id (both nodes None: not asked for). unit_nodes is the home side (the home
        post is one of its posts), assigned_nodes the assigned side, live nodes only (membership_nodes(live_only=True)):
        a user with a role assignment at one of its posts or groups, the role not archived. IN (subquery) lists a user
        once. The caller's access (_apply_scope) is a separate condition: this one narrows, never widens."""
        if unit_nodes is None and assigned_nodes is None:
            return query
        home = User.post_id.in_(sorted(unit_nodes.posts)) if unit_nodes is not None else false()
        attached = []
        if assigned_nodes is not None:
            if assigned_nodes.posts:
                attached.append(RoleAssignment.scope_post_id.in_(sorted(assigned_nodes.posts)))
            if assigned_nodes.groups:
                attached.append(RoleAssignment.scope_group_id.in_(sorted(assigned_nodes.groups)))
        assigned = User.id.in_(
            select(RoleAssignment.user_id).join(Role, Role.id == RoleAssignment.role_id)
            .where(Role.is_deleted == False, or_(*attached))    # noqa: E712
        ) if attached else false()
        if params.membership == "home":
            return query.where(home)
        if params.membership == "assigned":
            return query.where(assigned)
        return query.where(or_(home, assigned))

    @classmethod
    def _apply_user_type(cls, query: Select, params: UserCriteria) -> Select:
        """users.user_type holds the value of the enum (a plain string column)"""
        return cls._apply_exact(query, User.user_type, params.user_type.value if params.user_type else None)

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: UserCriteria,
        access_post_ids: Optional[Iterable[int]] = None,
        archived_post_ids: Optional[FrozenSet[int]] = None,
        caller_id: Optional[int] = None,
        *,
        unit_nodes: Optional[UnitNodes],
        assigned_nodes: Optional[UnitNodes],
    ) -> Select:
        """Apply all filters from UserCriteria (scope: _apply_scope); unit_nodes / assigned_nodes: the nodes of
        post_id, corps_id and brigade_id for the home side and the assigned side (both None: no unit filter)"""
        params = cls.criteria(params)

        # Archive state and access control
        query = cls._apply_scope(query, params, access_post_ids, archived_post_ids, caller_id)

        # ID filters
        query = cls._apply_exact(query, User.id, params.id)
        query = cls._apply_membership(query, params, unit_nodes, assigned_nodes)

        # Username filters
        query = cls._apply_exact(query, User.username, params.username)
        query = cls._apply_contains(query, User.username, params.username_contains)
        query = cls._apply_exact(query, User.username_alias, params.username_alias)
        query = cls._apply_contains(query, User.username_alias, params.username_alias_contains)

        # Status
        query = cls._apply_user_type(query, params)
        query = cls._apply_exact(query, User.is_active, params.is_active)
        if params.is_online is not None:
            online_ids = online_user_ids_select()
            query = query.where(User.id.in_(online_ids) if params.is_online else ~User.id.in_(online_ids))

        # Date ranges
        query = cls._apply_range(query, User.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_range(query, User.last_login_at, params.last_login_from, params.last_login_to)

        # Sorting & pagination
        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: UserCriteria,
        access_post_ids: Optional[Iterable[int]] = None,
        archived_post_ids: Optional[FrozenSet[int]] = None,
        caller_id: Optional[int] = None,
        *,
        unit_nodes: Optional[UnitNodes],
        assigned_nodes: Optional[UnitNodes],
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_scope(query, params, access_post_ids, archived_post_ids, caller_id)
        query = cls._apply_exact(query, User.id, params.id)
        query = cls._apply_membership(query, params, unit_nodes, assigned_nodes)
        query = cls._apply_exact(query, User.username, params.username)
        query = cls._apply_contains(query, User.username, params.username_contains)
        query = cls._apply_exact(query, User.username_alias, params.username_alias)
        query = cls._apply_contains(query, User.username_alias, params.username_alias_contains)
        query = cls._apply_user_type(query, params)
        query = cls._apply_exact(query, User.is_active, params.is_active)
        if params.is_online is not None:
            online_ids = online_user_ids_select()
            query = query.where(User.id.in_(online_ids) if params.is_online else ~User.id.in_(online_ids))
        query = cls._apply_range(query, User.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_range(query, User.last_login_at, params.last_login_from, params.last_login_to)
        return query
