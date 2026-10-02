"""
MediaFilterService - Filter service for MediaFile entity
"""

from typing import Optional, Iterable, get_args
from sqlalchemy import and_, not_, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Select

from app.backend.models.media import MediaFile
from app.backend.enums.general import MediaType, PosterStatus
from app.backend.models.observation import Observation
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.media_params import MediaFilterParams, MediaSortField


class MediaFilterService(SimpleFilterEngine):
    """Filter service for MediaFile entity"""

    MODEL = MediaFile
    PARAMS = MediaFilterParams
    SORTABLE_FIELDS = list(get_args(MediaSortField))

    @staticmethod
    def _apply_has_thumbnail(query: Select, has_thumbnail: Optional[bool]) -> Select:
        """Images always have a thumbnail (rendered on demand); videos once their poster is ready"""
        if has_thumbnail is None:
            return query
        ready = or_(
            MediaFile.media_type == MediaType.IMAGE.value,
            and_(MediaFile.media_type == MediaType.VIDEO.value, MediaFile.poster_status == PosterStatus.READY.value),
        )
        return query.where(ready) if has_thumbnail else query.where(not_(ready))

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: MediaFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from MediaFilterParams"""
        query = query.options(selectinload(MediaFile.user))

        # MediaFile has no is_deleted

        # Access control through observation's post_id
        if access_post_ids is not None:
            query = query.join(Observation, MediaFile.observation_id == Observation.id)
            query = query.where(Observation.post_id.in_(list(access_post_ids)))

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
        params: MediaFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        if access_post_ids is not None:
            query = query.join(Observation, MediaFile.observation_id == Observation.id)
            query = query.where(Observation.post_id.in_(list(access_post_ids)))

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
