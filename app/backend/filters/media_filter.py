"""
MediaFilterService - Filter service for MediaFile entity
"""

from typing import Optional
from sqlalchemy.sql.selectable import Select

from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.media_params import MediaFilterParams


class MediaFilterService(SimpleFilterEngine):
    """Filter service for MediaFile entity"""

    MODEL = MediaFile
    PARAMS = MediaFilterParams
    SORTABLE_FIELDS = ["id", "original_filename", "file_size", "media_type", "uploaded_at", "observation_id"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: MediaFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply all filters from MediaFilterParams"""

        # MediaFile has no is_deleted

        # Access control through observation's post_id
        if access_post_id is not None:
            query = query.join(Observation, MediaFile.observation_id == Observation.id)
            query = query.where(Observation.post_id == access_post_id)

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

        # Has thumbnail filter
        if params.has_thumbnail is True:
            query = query.where(MediaFile.thumbnail_path.isnot(None))
        elif params.has_thumbnail is False:
            query = query.where(MediaFile.thumbnail_path.is_(None))

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: MediaFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply filters without pagination"""
        if access_post_id is not None:
            query = query.join(Observation, MediaFile.observation_id == Observation.id)
            query = query.where(Observation.post_id == access_post_id)

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

        if params.has_thumbnail is True:
            query = query.where(MediaFile.thumbnail_path.isnot(None))
        elif params.has_thumbnail is False:
            query = query.where(MediaFile.thumbnail_path.is_(None))

        return query
