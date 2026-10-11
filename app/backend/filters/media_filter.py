"""
MediaFilterService - Filter service for MediaFile entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable
from sqlalchemy import and_, not_, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Select

from app.backend.config import settings
from app.backend.models.media import MediaFile
from app.backend.enums.general import MediaType, PosterStatus
from app.backend.models.observation import Observation
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class MediaCriteria(ListCriteria):
    """What MediaFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    observation_id: Optional[int] = None
    original_filename_contains: Optional[str] = None
    media_type: Optional[str] = None
    mime_type: Optional[str] = None
    file_extension: Optional[str] = None
    file_size_from: Optional[int] = None
    file_size_to: Optional[int] = None
    width_from: Optional[int] = None
    width_to: Optional[int] = None
    height_from: Optional[int] = None
    height_to: Optional[int] = None
    has_thumbnail: Optional[bool] = None
    uploaded_at_from: Optional[datetime] = None
    uploaded_at_to: Optional[datetime] = None
    include_deleted: bool = False


class MediaFilterService(SimpleFilterEngine):
    """Filter service for MediaFile entity"""

    MODEL = MediaFile
    CRITERIA = MediaCriteria
    SORTABLE_FIELDS = ["id", "original_filename", "file_size", "media_type", "uploaded_at", "observation_id"]

    @staticmethod
    def _apply_has_thumbnail(query: Select, has_thumbnail: Optional[bool]) -> Select:
        """Images have a thumbnail (rendered on demand) unless imgproxy refuses them as too large, the rule of
        MediaFile.too_large_to_render (an unknown size counts as renderable); videos once their poster is ready"""
        if has_thumbnail is None:
            return query
        too_large = and_(
            MediaFile.width.is_not(None), MediaFile.width > 0,
            MediaFile.height.is_not(None), MediaFile.height > 0,
            MediaFile.width * MediaFile.height > settings.storage.imgproxy.max_src_resolution_mp * 1_000_000,
        )
        ready = or_(
            and_(MediaFile.media_type == MediaType.IMAGE.value, not_(too_large)),
            and_(MediaFile.media_type == MediaType.VIDEO.value, MediaFile.poster_status == PosterStatus.READY.value),
        )
        return query.where(ready) if has_thumbnail else query.where(not_(ready))

    @classmethod
    def _apply_scope(cls, query: Select, params: MediaCriteria, access_post_ids: Optional[Iterable[int]]) -> Select:
        """Archived media only on include_deleted; media of an archived observation never (the media routes answer
        404 for it); the post scope through the observation's post_id"""
        query = cls._apply_soft_delete(query, params)
        query = query.join(Observation, MediaFile.observation_id == Observation.id)
        query = query.where(Observation.is_deleted == False)
        if access_post_ids is not None:
            query = query.where(Observation.post_id.in_(list(access_post_ids)))
        return query

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: MediaCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from MediaCriteria"""
        params = cls.criteria(params)
        query = query.options(selectinload(MediaFile.user))

        query = cls._apply_scope(query, params, access_post_ids)

        query = cls._apply_exact(query, MediaFile.id, params.id)
        query = cls._apply_exact(query, MediaFile.observation_id, params.observation_id)
        query = cls._apply_contains(query, MediaFile.original_filename, params.original_filename_contains)
        query = cls._apply_exact(query, MediaFile.media_type, params.media_type)
        query = cls._apply_exact(query, MediaFile.mime_type, params.mime_type)
        query = cls._apply_exact(query, MediaFile.file_extension, params.file_extension)
        query = cls._apply_range(query, MediaFile.file_size, params.file_size_from, params.file_size_to)
        query = cls._apply_range(query, MediaFile.width, params.width_from, params.width_to)
        query = cls._apply_range(query, MediaFile.height, params.height_from, params.height_to)
        query = cls._apply_range(query, MediaFile.uploaded_at, params.uploaded_at_from, params.uploaded_at_to)

        query = cls._apply_has_thumbnail(query, params.has_thumbnail)

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: MediaCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        query = cls._apply_scope(query, params, access_post_ids)

        query = cls._apply_exact(query, MediaFile.id, params.id)
        query = cls._apply_exact(query, MediaFile.observation_id, params.observation_id)
        query = cls._apply_contains(query, MediaFile.original_filename, params.original_filename_contains)
        query = cls._apply_exact(query, MediaFile.media_type, params.media_type)
        query = cls._apply_exact(query, MediaFile.mime_type, params.mime_type)
        query = cls._apply_exact(query, MediaFile.file_extension, params.file_extension)
        query = cls._apply_range(query, MediaFile.file_size, params.file_size_from, params.file_size_to)
        query = cls._apply_range(query, MediaFile.width, params.width_from, params.width_to)
        query = cls._apply_range(query, MediaFile.height, params.height_from, params.height_to)
        query = cls._apply_range(query, MediaFile.uploaded_at, params.uploaded_at_from, params.uploaded_at_to)

        query = cls._apply_has_thumbnail(query, params.has_thumbnail)

        return query
