"""
MediaFormatFilterService - Filter service for MediaFormat entity
"""

from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.media_format import MediaFormat
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.media_format_params import MediaFormatFilterParams


class MediaFormatFilterService(SimpleFilterEngine):
    """Filter service for MediaFormat entity"""

    MODEL = MediaFormat
    PARAMS = MediaFormatFilterParams
    SORTABLE_FIELDS = ["id", "media_type", "content_type", "label", "max_file_size_bytes", "sort_order", "is_active"]
    ADMIN_ONLY_PARAMS = ["include_deleted"]

    @classmethod
    def _apply_common(cls, query: Select, params: MediaFormatFilterParams) -> Select:
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, MediaFormat.id, params.id)
        query = cls._apply_exact(query, MediaFormat.media_type, params.media_type.lower() if params.media_type else None)
        query = cls._apply_exact(query, MediaFormat.content_type, params.content_type.lower() if params.content_type else None)
        query = cls._apply_contains(query, MediaFormat.content_type, params.content_type_contains)
        query = cls._apply_contains(query, MediaFormat.label, params.label_contains)
        query = cls._apply_exact(query, MediaFormat.is_active, params.is_active)
        return query

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: MediaFormatFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        query = cls._apply_common(query, params)
        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)
        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: MediaFormatFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        return cls._apply_common(query, params)
