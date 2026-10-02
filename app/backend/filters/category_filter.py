"""
CategoryFilterService - Filter service for Category entity
"""

from typing import Optional, Iterable, get_args
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import Category
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.category_params import CategoryFilterParams, CategorySortField


class CategoryFilterService(SimpleFilterEngine):
    """Filter service for Category entity"""

    MODEL = Category
    PARAMS = CategoryFilterParams
    SORTABLE_FIELDS = list(get_args(CategorySortField))

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: CategoryFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from CategoryFilterParams"""

        query = cls._apply_soft_delete(query, params)

        query = cls._apply_exact(query, Category.id, params.id)
        query = cls._apply_exact(query, Category.name, params.name)
        query = cls._apply_contains(query, Category.name, params.name_contains)
        query = cls._apply_exact(query, Category.alias, params.alias)
        query = cls._apply_contains(query, Category.alias, params.alias_contains)

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: CategoryFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, Category.id, params.id)
        query = cls._apply_exact(query, Category.name, params.name)
        query = cls._apply_contains(query, Category.name, params.name_contains)
        query = cls._apply_exact(query, Category.alias, params.alias)
        query = cls._apply_contains(query, Category.alias, params.alias_contains)
        return query
