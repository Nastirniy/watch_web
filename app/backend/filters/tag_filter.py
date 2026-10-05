"""
TagFilterService - Filter service for Tag entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import Tag
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class TagCriteria(ListCriteria):
    """What TagFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    name: Optional[str] = None
    name_contains: Optional[str] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None
    include_deleted: bool = False


class TagFilterService(SimpleFilterEngine):
    """Filter service for Tag entity"""

    MODEL = Tag
    CRITERIA = TagCriteria
    SORTABLE_FIELDS = ["id", "name", "created_at"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: TagCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from TagCriteria"""
        params = cls.criteria(params)

        query = cls._apply_soft_delete(query, params)

        query = cls._apply_exact(query, Tag.id, params.id)
        query = cls._apply_exact(query, Tag.name, params.name)
        query = cls._apply_contains(query, Tag.name, params.name_contains)
        query = cls._apply_range(query, Tag.created_at, params.created_at_from, params.created_at_to)

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: TagCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, Tag.id, params.id)
        query = cls._apply_exact(query, Tag.name, params.name)
        query = cls._apply_contains(query, Tag.name, params.name_contains)
        query = cls._apply_range(query, Tag.created_at, params.created_at_from, params.created_at_to)
        return query
