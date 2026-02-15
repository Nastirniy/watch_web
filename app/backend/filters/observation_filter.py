"""
ObservationFilterService - Filter service for Observation entity
Uses simple typed params for clear FastAPI docs
"""

from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Select
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.observation import Observation, ObservationTag
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.observation_params import ObservationFilterParams


class ObservationFilterService(SimpleFilterEngine):
    """Filter service for Observation entity"""

    MODEL = Observation
    PARAMS = ObservationFilterParams
    SORTABLE_FIELDS = [
        "id", "observed_at", "created_at", "updated_at",
        "frequency_center", "video_frequency_center", "status", "post_id"
    ]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: ObservationFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply all filters from ObservationFilterParams"""

        # Soft delete
        query = cls._apply_soft_delete(query, params)

        # Access control (non-admin sees only their post)
        if access_post_id is not None:
            query = query.where(Observation.post_id == access_post_id)

        # === ID Filters ===
        query = cls._apply_exact(query, Observation.id, params.id)
        query = cls._apply_exact(query, Observation.user_id, params.user_id)
        query = cls._apply_exact(query, Observation.post_id, params.post_id)
        query = cls._apply_exact(query, Observation.signal_type_id, params.signal_type_id)

        # === Status Filters ===
        query = cls._apply_exact(query, Observation.status, params.status)
        if params.status_in:
            status_list = params.get_status_list()
            query = cls._apply_in_list(query, Observation.status, status_list)
        query = cls._apply_exact(query, Observation.confidence_level, params.confidence_level)

        # === Frequency Filters ===
        query = cls._apply_range(query, Observation.frequency_center,
                                  params.frequency_center_from, params.frequency_center_to)
        query = cls._apply_range(query, Observation.bandwidth,
                                  params.bandwidth_from, params.bandwidth_to)

        # === Video Frequency Filters ===
        query = cls._apply_range(query, Observation.video_frequency_center,
                                  params.video_frequency_center_from, params.video_frequency_center_to)

        # === Date Filters ===
        if params.last_n_days is not None:
            start_date = cls._get_last_n_days_start(params.last_n_days)
            query = query.where(Observation.observed_at >= start_date)
        else:
            query = cls._apply_range(query, Observation.observed_at,
                                      params.observed_at_from, params.observed_at_to)

        query = cls._apply_range(query, Observation.created_at,
                                  params.created_at_from, params.created_at_to)

        # === Disappeared Filters ===
        if params.is_disappeared is True:
            query = query.where(Observation.disappeared_at.isnot(None))
        elif params.is_disappeared is False:
            query = query.where(Observation.disappeared_at.is_(None))
        query = cls._apply_exact(query, Observation.disappeared_by, params.disappeared_by)

        # === Duration Filters ===
        query = cls._apply_range(query, Observation.duration_seconds,
                                  params.duration_from, params.duration_to)

        # === Comment Search ===
        query = cls._apply_contains(query, Observation.comment, params.comment_contains)

        # === Tag Filters (AND logic - must have ALL tags) ===
        if params.tag_ids:
            tag_id_list = params.get_tag_id_list()
            if tag_id_list:
                for tag_id in tag_id_list:
                    query = query.where(
                        Observation.id.in_(
                            select(ObservationTag.observation_id)
                            .where(ObservationTag.tag_id == tag_id)
                        )
                    )

        # === Sorting & Pagination ===
        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        # Eager load relationships
        query = query.options(selectinload(Observation.media_files))
        query = query.options(selectinload(Observation.tags))

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: ObservationFilterParams,
        access_post_id: Optional[int] = None
    ) -> Select:
        """Apply filters without pagination (for counting)"""
        # Same as apply_filters but without pagination and eager loading
        query = cls._apply_soft_delete(query, params)

        if access_post_id is not None:
            query = query.where(Observation.post_id == access_post_id)

        query = cls._apply_exact(query, Observation.id, params.id)
        query = cls._apply_exact(query, Observation.user_id, params.user_id)
        query = cls._apply_exact(query, Observation.post_id, params.post_id)
        query = cls._apply_exact(query, Observation.signal_type_id, params.signal_type_id)
        query = cls._apply_exact(query, Observation.status, params.status)

        if params.status_in:
            status_list = params.get_status_list()
            query = cls._apply_in_list(query, Observation.status, status_list)

        query = cls._apply_exact(query, Observation.confidence_level, params.confidence_level)
        query = cls._apply_range(query, Observation.frequency_center,
                                  params.frequency_center_from, params.frequency_center_to)
        query = cls._apply_range(query, Observation.bandwidth,
                                  params.bandwidth_from, params.bandwidth_to)
        query = cls._apply_range(query, Observation.video_frequency_center,
                                  params.video_frequency_center_from, params.video_frequency_center_to)

        if params.last_n_days is not None:
            start_date = cls._get_last_n_days_start(params.last_n_days)
            query = query.where(Observation.observed_at >= start_date)
        else:
            query = cls._apply_range(query, Observation.observed_at,
                                      params.observed_at_from, params.observed_at_to)

        query = cls._apply_range(query, Observation.created_at,
                                  params.created_at_from, params.created_at_to)

        if params.is_disappeared is True:
            query = query.where(Observation.disappeared_at.isnot(None))
        elif params.is_disappeared is False:
            query = query.where(Observation.disappeared_at.is_(None))

        query = cls._apply_exact(query, Observation.disappeared_by, params.disappeared_by)
        query = cls._apply_range(query, Observation.duration_seconds,
                                  params.duration_from, params.duration_to)
        query = cls._apply_contains(query, Observation.comment, params.comment_contains)

        if params.tag_ids:
            tag_id_list = params.get_tag_id_list()
            if tag_id_list:
                for tag_id in tag_id_list:
                    query = query.where(
                        Observation.id.in_(
                            select(ObservationTag.observation_id)
                            .where(ObservationTag.tag_id == tag_id)
                        )
                    )

        return query
