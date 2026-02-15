"""
SignalTypeFilterService - Filter service for SignalType entity
"""

from typing import Optional
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import SignalType
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.signal_type_params import SignalTypeFilterParams


class SignalTypeFilterService(SimpleFilterEngine):
    """Filter service for SignalType entity"""

    MODEL = SignalType
    PARAMS = SignalTypeFilterParams
    SORTABLE_FIELDS = ["id", "code", "name", "sort_order", "created_at", "updated_at", "is_active", "category_id"]
    ADMIN_ONLY_PARAMS = ["include_deleted"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: SignalTypeFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply all filters from SignalTypeFilterParams"""

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
        params: SignalTypeFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply filters without pagination"""
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
