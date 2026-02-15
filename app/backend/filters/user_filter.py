"""
UserFilterService - Filter service for User entity
"""

from typing import Optional
from sqlalchemy.sql.selectable import Select

from app.backend.models.user import User
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.user_params import UserFilterParams


class UserFilterService(SimpleFilterEngine):
    """Filter service for User entity"""

    MODEL = User
    PARAMS = UserFilterParams
    SORTABLE_FIELDS = ["id", "username", "username_alias", "created_at", "updated_at", "last_login_at", "is_active"]
    ADMIN_ONLY_SORTABLE = []
    ADMIN_ONLY_PARAMS = ["post_id", "include_deleted"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: UserFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply all filters from UserFilterParams"""

        query = cls._apply_soft_delete(query, params)

        # Access control
        if access_post_id is not None:
            query = query.where(User.post_id == access_post_id)

        # ID filters
        query = cls._apply_exact(query, User.id, params.id)
        query = cls._apply_exact(query, User.post_id, params.post_id)

        # Username filters
        query = cls._apply_exact(query, User.username, params.username)
        query = cls._apply_contains(query, User.username, params.username_contains)
        query = cls._apply_exact(query, User.username_alias, params.username_alias)
        query = cls._apply_contains(query, User.username_alias, params.username_alias_contains)

        # Status
        query = cls._apply_exact(query, User.is_active, params.is_active)

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
        params: UserFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply filters without pagination"""
        query = cls._apply_soft_delete(query, params)
        if access_post_id is not None:
            query = query.where(User.post_id == access_post_id)
        query = cls._apply_exact(query, User.id, params.id)
        query = cls._apply_exact(query, User.post_id, params.post_id)
        query = cls._apply_exact(query, User.username, params.username)
        query = cls._apply_contains(query, User.username, params.username_contains)
        query = cls._apply_exact(query, User.username_alias, params.username_alias)
        query = cls._apply_contains(query, User.username_alias, params.username_alias_contains)
        query = cls._apply_exact(query, User.is_active, params.is_active)
        query = cls._apply_range(query, User.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_range(query, User.last_login_at, params.last_login_from, params.last_login_to)
        return query
