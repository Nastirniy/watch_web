"""
UserFilterService - Filter service for User entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, Optional, Iterable
from sqlalchemy import or_
from sqlalchemy.sql.selectable import Select

from app.backend.enums.general import UserType
from app.backend.models.user import User
from app.backend.auth.presence import online_user_ids_select
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class UserCriteria(ListCriteria):
    """What UserFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    username: Optional[str] = None
    username_contains: Optional[str] = None
    username_alias: Optional[str] = None
    username_alias_contains: Optional[str] = None
    post_id: Optional[int] = None
    corps_id: Optional[int] = None      # the route turns units into unit_post_ids (services/units.py units_filter)
    brigade_id: Optional[int] = None
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
        unit_post_ids: Optional[FrozenSet[int]],
    ) -> Select:
        """Apply all filters from UserCriteria (scope: _apply_scope); unit_post_ids: the posts of the units asked
        for (corps_id, brigade_id; None: no unit filter), matched against the home post"""
        params = cls.criteria(params)

        # Archive state and access control
        query = cls._apply_scope(query, params, access_post_ids, archived_post_ids, caller_id)

        # ID filters
        query = cls._apply_exact(query, User.id, params.id)
        query = cls._apply_exact(query, User.post_id, params.post_id)
        if unit_post_ids is not None:   # home post in the units asked for: the caller is not listed outside them
            query = query.where(User.post_id.in_(sorted(unit_post_ids)))

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
        unit_post_ids: Optional[FrozenSet[int]],
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_scope(query, params, access_post_ids, archived_post_ids, caller_id)
        query = cls._apply_exact(query, User.id, params.id)
        query = cls._apply_exact(query, User.post_id, params.post_id)
        if unit_post_ids is not None:   # home post in the units asked for: the caller is not listed outside them
            query = query.where(User.post_id.in_(sorted(unit_post_ids)))
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
