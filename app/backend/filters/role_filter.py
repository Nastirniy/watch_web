"""
RoleFilterService - Filter service for Role entity
"""

from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.role import Role
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.role_params import RoleFilterParams


class RoleFilterService(SimpleFilterEngine):
    """Filter service for Role entity"""

    MODEL = Role
    PARAMS = RoleFilterParams
    SORTABLE_FIELDS = ["id", "name", "display_name", "created_at", "updated_at"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: RoleFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from RoleFilterParams"""

        query = cls._apply_soft_delete(query, params)

        query = cls._apply_exact(query, Role.id, params.id)
        query = cls._apply_exact(query, Role.name, params.name)
        query = cls._apply_contains(query, Role.name, params.name_contains)
        query = cls._apply_exact(query, Role.display_name, params.display_name)
        query = cls._apply_contains(query, Role.display_name, params.display_name_contains)
        query = cls._apply_contains(query, Role.description, params.description_contains)
        query = cls._apply_range(query, Role.created_at, params.created_at_from, params.created_at_to)

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: RoleFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, Role.id, params.id)
        query = cls._apply_exact(query, Role.name, params.name)
        query = cls._apply_contains(query, Role.name, params.name_contains)
        query = cls._apply_exact(query, Role.display_name, params.display_name)
        query = cls._apply_contains(query, Role.display_name, params.display_name_contains)
        query = cls._apply_contains(query, Role.description, params.description_contains)
        query = cls._apply_range(query, Role.created_at, params.created_at_from, params.created_at_to)
        return query
