"""
TagFilterService - Filter service for Tag entity
"""

from typing import Optional, Iterable, get_args
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import Tag
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.tag_params import TagFilterParams, TagSortField


class TagFilterService(SimpleFilterEngine):
    """Filter service for Tag entity"""

    MODEL = Tag
    PARAMS = TagFilterParams
    SORTABLE_FIELDS = list(get_args(TagSortField))

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: TagFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from TagFilterParams"""

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
        params: TagFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, Tag.id, params.id)
        query = cls._apply_exact(query, Tag.name, params.name)
        query = cls._apply_contains(query, Tag.name, params.name_contains)
        query = cls._apply_range(query, Tag.created_at, params.created_at_from, params.created_at_to)
        return query
