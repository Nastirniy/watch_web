"""
CategoryFilterService - Filter service for Category entity
"""

from dataclasses import dataclass
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import Category
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class CategoryCriteria(ListCriteria):
    """What CategoryFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    name: Optional[str] = None
    name_contains: Optional[str] = None
    alias: Optional[str] = None
    alias_contains: Optional[str] = None
    include_deleted: bool = False


class CategoryFilterService(SimpleFilterEngine):
    """Filter service for Category entity"""

    MODEL = Category
    CRITERIA = CategoryCriteria
    SORTABLE_FIELDS = ["id", "name", "alias"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: CategoryCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from CategoryCriteria"""
        params = cls.criteria(params)

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
        params: CategoryCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, Category.id, params.id)
        query = cls._apply_exact(query, Category.name, params.name)
        query = cls._apply_contains(query, Category.name, params.name_contains)
        query = cls._apply_exact(query, Category.alias, params.alias)
        query = cls._apply_contains(query, Category.alias, params.alias_contains)
        return query
