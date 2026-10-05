"""
SignalTypeFilterService - Filter service for SignalType entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import SignalType
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class SignalTypeCriteria(ListCriteria):
    """What SignalTypeFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    code: Optional[str] = None
    code_contains: Optional[str] = None
    name: Optional[str] = None
    name_contains: Optional[str] = None
    description_contains: Optional[str] = None
    category_id: Optional[int] = None
    is_active: Optional[bool] = None
    sort_order_from: Optional[int] = None
    sort_order_to: Optional[int] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None
    include_deleted: bool = False


class SignalTypeFilterService(SimpleFilterEngine):
    """Filter service for SignalType entity"""

    MODEL = SignalType
    CRITERIA = SignalTypeCriteria
    SORTABLE_FIELDS = ["id", "code", "name", "sort_order", "created_at", "updated_at", "is_active", "category_id"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: SignalTypeCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from SignalTypeCriteria"""
        params = cls.criteria(params)

        query = cls._apply_soft_delete(query, params)

        query = cls._apply_exact(query, SignalType.id, params.id)
        query = cls._apply_exact(query, SignalType.code, params.code)
        query = cls._apply_contains(query, SignalType.code, params.code_contains)
        query = cls._apply_exact(query, SignalType.name, params.name)
        query = cls._apply_contains(query, SignalType.name, params.name_contains)
        query = cls._apply_contains(query, SignalType.description, params.description_contains)
        query = cls._apply_exact(query, SignalType.category_id, params.category_id)
        query = cls._apply_exact(query, SignalType.is_active, params.is_active)
        query = cls._apply_range(query, SignalType.sort_order, params.sort_order_from, params.sort_order_to)
        query = cls._apply_range(query, SignalType.created_at, params.created_at_from, params.created_at_to)

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: SignalTypeCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, SignalType.id, params.id)
        query = cls._apply_exact(query, SignalType.code, params.code)
        query = cls._apply_contains(query, SignalType.code, params.code_contains)
        query = cls._apply_exact(query, SignalType.name, params.name)
        query = cls._apply_contains(query, SignalType.name, params.name_contains)
        query = cls._apply_contains(query, SignalType.description, params.description_contains)
        query = cls._apply_exact(query, SignalType.category_id, params.category_id)
        query = cls._apply_exact(query, SignalType.is_active, params.is_active)
        query = cls._apply_range(query, SignalType.sort_order, params.sort_order_from, params.sort_order_to)
        query = cls._apply_range(query, SignalType.created_at, params.created_at_from, params.created_at_to)
        return query
