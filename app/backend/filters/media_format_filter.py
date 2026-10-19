"""
MediaFormatFilterService - Filter service for MediaFormat entity
"""

from dataclasses import dataclass
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.enums.general import MediaType
from app.backend.models.media_format import MediaFormat
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class MediaFormatCriteria(ListCriteria):
    """What MediaFormatFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    media_type: Optional[MediaType] = None
    content_type: Optional[str] = None
    content_type_contains: Optional[str] = None
    label_contains: Optional[str] = None
    is_active: Optional[bool] = None
    include_deleted: bool = False


class MediaFormatFilterService(SimpleFilterEngine):
    """Filter service for MediaFormat entity"""

    MODEL = MediaFormat
    CRITERIA = MediaFormatCriteria
    SORTABLE_FIELDS = ["id", "media_type", "content_type", "label", "max_file_size_bytes", "sort_order", "is_active"]

    @classmethod
    def _apply_common(cls, query: Select, params: MediaFormatCriteria) -> Select:
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_exact(query, MediaFormat.id, params.id)
        query = cls._apply_exact(query, MediaFormat.media_type, params.media_type)
        query = cls._apply_exact(query, MediaFormat.content_type, params.content_type.lower() if params.content_type else None)
        query = cls._apply_contains(query, MediaFormat.content_type, params.content_type_contains)
        query = cls._apply_contains(query, MediaFormat.label, params.label_contains)
        query = cls._apply_exact(query, MediaFormat.is_active, params.is_active)
        return query

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: MediaFormatCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        params = cls.criteria(params)
        query = cls._apply_common(query, params)
        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)
        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: MediaFormatCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        params = cls.criteria(params)
        return cls._apply_common(query, params)
