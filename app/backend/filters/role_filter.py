"""
RoleFilterService - Filter service for Role entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.role import Role
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class RoleCriteria(ListCriteria):
    """What RoleFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    include_deleted: bool = False
    name: Optional[str] = None
    name_contains: Optional[str] = None
    display_name: Optional[str] = None
    display_name_contains: Optional[str] = None
    description_contains: Optional[str] = None
    is_builtin: Optional[bool] = None
    kind: Optional[str] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None


class RoleFilterService(SimpleFilterEngine):
    """Filter service for Role entity"""

    MODEL = Role
    CRITERIA = RoleCriteria
    SORTABLE_FIELDS = ["id", "name", "display_name", "created_at", "updated_at"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: RoleCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from RoleCriteria"""
        params = cls.criteria(params)

        query = cls._apply_soft_delete(query, params)

        query = cls._apply_exact(query, Role.id, params.id)
        query = cls._apply_exact(query, Role.name, params.name)
        query = cls._apply_contains(query, Role.name, params.name_contains)
        query = cls._apply_exact(query, Role.display_name, params.display_name)
        query = cls._apply_contains(query, Role.display_name, params.display_name_contains)
        query = cls._apply_contains(query, Role.description, params.description_contains)
        query = cls._apply_range(query, Role.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_builtin(query, params.is_builtin)
        query = cls._apply_exact(query, Role.kind, getattr(params.kind, "value", params.kind))

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: RoleCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, Role.id, params.id)
        query = cls._apply_exact(query, Role.name, params.name)
        query = cls._apply_contains(query, Role.name, params.name_contains)
        query = cls._apply_exact(query, Role.display_name, params.display_name)
        query = cls._apply_contains(query, Role.display_name, params.display_name_contains)
        query = cls._apply_contains(query, Role.description, params.description_contains)
        query = cls._apply_range(query, Role.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_builtin(query, params.is_builtin)
        query = cls._apply_exact(query, Role.kind, getattr(params.kind, "value", params.kind))
        return query

    @staticmethod
    def _apply_builtin(query: Select, is_builtin: Optional[bool]) -> Select:
        if is_builtin is None:
            return query
        return query.where(Role.builtin_key.is_not(None) if is_builtin else Role.builtin_key.is_(None))
