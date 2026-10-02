"""
ObservationFilterService - Filter service for Observation entity
Uses simple typed params for clear FastAPI docs
"""

from typing import Optional, List, Dict, Iterable
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Select
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.observation import Observation, ObservationTag
from app.backend.filters.observation_frequency_filter import ObservationFrequencyFilter
from app.backend.filters.simple_engine import SimpleFilterEngine
from app.backend.filters.params.observation_params import ObservationFilterParams


class ObservationFilterService(SimpleFilterEngine):
    """Filter service for Observation entity"""

    MODEL = Observation
    PARAMS = ObservationFilterParams
    SORTABLE_FIELDS = [
        "id", "observed_at", "created_at", "updated_at", "status", "post_id",
        "frequency_center",
    ]
    # frequency_center is not a column of observations: an observation is sorted by the lowest
    # center among its active bands that match the band filters (frequency_kind, center and
    # bandwidth ranges). This is a correlated subquery per observation and the sort cannot use
    # an index, so it is expensive on a large table: combine it with narrowing filters and
    # pagination. Prefer observed_at / created_at for plain listings.
    EXPRESSION_SORTABLE = ["frequency_center"]

    @classmethod
    def _band_conditions(cls, params: ObservationFilterParams) -> list:
        return ObservationFrequencyFilter.band_conditions(
            kind=params.frequency_kind,
            center_from=params.frequency_center_from,
            center_to=params.frequency_center_to,
            bandwidth_from=params.bandwidth_from,
            bandwidth_to=params.bandwidth_to,
        )

    @classmethod
    def _sort_expression(cls, name: str, params: ObservationFilterParams):
        if name == "frequency_center":
            return ObservationFrequencyFilter.lowest_center(cls._band_conditions(params))
        return super()._sort_expression(name, params)

    @classmethod
    def _apply_frequency_filters(cls, query: Select, params: ObservationFilterParams) -> Select:
        """Every band condition goes into one subquery, so they must hold for the same active band"""
        conditions = cls._band_conditions(params)
        if conditions:
            query = query.where(ObservationFrequencyFilter.observation_has_band(conditions))
        return query

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: ObservationFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from ObservationFilterParams"""

        # Soft delete
        query = cls._apply_soft_delete(query, params)

        # Access control (non-admin sees only their post)
        if access_post_ids is not None:
            query = query.where(Observation.post_id.in_(list(access_post_ids)))

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
        query = cls._apply_frequency_filters(query, params)

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

        # === Tag Filters (OR logic - must have ANY of the tags) ===
        if params.tag_ids:
            tag_id_list = params.get_tag_id_list()
            if tag_id_list:
                query = query.where(
                    Observation.id.in_(
                        select(ObservationTag.observation_id)
                        .where(ObservationTag.tag_id.in_(tag_id_list))
                    )
                )

        # === Sorting & Pagination ===
        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        # Eager load relationships
        query = query.options(selectinload(Observation.media_files))
        query = query.options(selectinload(Observation.tags))
        query = query.options(selectinload(Observation.user))
        query = query.options(selectinload(Observation.disappeared_by_user))
        query = query.options(selectinload(Observation.all_frequencies))

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: ObservationFilterParams,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination (for counting)"""
        # Same as apply_filters but without pagination and eager loading
        query = cls._apply_soft_delete(query, params)

        if access_post_ids is not None:
            query = query.where(Observation.post_id.in_(list(access_post_ids)))

        query = cls._apply_exact(query, Observation.id, params.id)
        query = cls._apply_exact(query, Observation.user_id, params.user_id)
        query = cls._apply_exact(query, Observation.post_id, params.post_id)
        query = cls._apply_exact(query, Observation.signal_type_id, params.signal_type_id)
        query = cls._apply_exact(query, Observation.status, params.status)

        if params.status_in:
            status_list = params.get_status_list()
            query = cls._apply_in_list(query, Observation.status, status_list)

        query = cls._apply_exact(query, Observation.confidence_level, params.confidence_level)
        query = cls._apply_frequency_filters(query, params)

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
                query = query.where(
                    Observation.id.in_(
                        select(ObservationTag.observation_id)
                        .where(ObservationTag.tag_id.in_(tag_id_list))
                    )
                )

        return query
