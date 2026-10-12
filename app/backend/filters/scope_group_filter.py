"""
ScopeGroupFilterService - Filter service for ScopeGroup entity
"""

from dataclasses import dataclass
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.scope_group import ScopeGroup
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class ScopeGroupCriteria(ListCriteria):
    """What ScopeGroupFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    parent_id: Optional[int] = None
    name_contains: Optional[str] = None
    include_deleted: bool = False


class ScopeGroupFilterService(SimpleFilterEngine):
    """Filter service for ScopeGroup entity"""

    MODEL = ScopeGroup
    CRITERIA = ScopeGroupCriteria
    SORTABLE_FIELDS = ["id", "name", "created_at", "updated_at"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: ScopeGroupCriteria,
        group_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from ScopeGroupCriteria. group_ids: the groups the caller sees (None: every group)"""
        params = cls.criteria(params)
        query = cls.apply_filters_for_count(query, params, group_ids)
        query = cls._apply_sorting(query, params)
        return cls._apply_pagination(query, params)

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: ScopeGroupCriteria,
        group_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without sorting and pagination"""
        params = cls.criteria(params)
        if group_ids is not None:
            query = query.where(ScopeGroup.id.in_(sorted(group_ids)))
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, ScopeGroup.id, params.id)
        query = cls._apply_exact(query, ScopeGroup.parent_id, params.parent_id)
        return cls._apply_contains(query, ScopeGroup.name, params.name_contains)
