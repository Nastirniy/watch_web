"""
ObservationFilterService - Filter service for Observation entity
Uses simple typed params for clear FastAPI docs
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import FrozenSet, Optional, Iterable, List
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select

from app.backend.models.observation import Observation
from app.backend.filters.observation_frequency_filter import ObservationFrequencyFilter
from app.backend.filters.observation_tag_filter import ObservationTagFilter
from app.backend.enums.general import ConfidenceLevel, FrequencyKind, ObservationStatus, TagMatch, UnitPlacement
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class ObservationCriteria(ListCriteria):
    """What ObservationFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    user_id: Optional[int] = None
    post_id: Optional[int] = None
    signal_type_id: Optional[int] = None
    corps_id: Optional[int] = None      # the route turns units into unit_condition (services/units.py)
    brigade_id: Optional[int] = None
    placement: UnitPlacement = UnitPlacement.RECORDED
    status: Optional[ObservationStatus] = None
    status_in: Optional[List[ObservationStatus]] = None
    confidence_level: Optional[ConfidenceLevel] = None
    frequency_kind: Optional[FrequencyKind] = None
    frequency_center_from: Optional[Decimal] = None
    frequency_center_to: Optional[Decimal] = None
    bandwidth_from: Optional[Decimal] = None
    bandwidth_to: Optional[Decimal] = None
    observed_at_from: Optional[datetime] = None
    observed_at_to: Optional[datetime] = None
    last_n_days: Optional[int] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None
    is_disappeared: Optional[bool] = None
    disappeared_by: Optional[int] = None
    duration_from: Optional[int] = None
    duration_to: Optional[int] = None
    comment_contains: Optional[str] = None
    tag_ids: Optional[List[int]] = None
    tag_match: TagMatch = TagMatch.ANY
    include_deleted: bool = False


class ObservationFilterService(SimpleFilterEngine):
    """Filter service for Observation entity"""

    MODEL = Observation
    CRITERIA = ObservationCriteria
    SORTABLE_FIELDS = ["id", "observed_at", "created_at", "updated_at", "status", "post_id", "frequency_center"]
    # frequency_center is not a column of observations: an observation is sorted by the lowest
    # center among its active bands that match the band filters (frequency_kind, center and
    # bandwidth ranges). This is a correlated subquery per observation and the sort cannot use
    # an index, so it is expensive on a large table: combine it with narrowing filters and
    # pagination. Prefer observed_at / created_at for plain listings.
    EXPRESSION_SORTABLE = ["frequency_center"]

    @classmethod
    def _band_conditions(cls, params: ObservationCriteria) -> list:
        return ObservationFrequencyFilter.band_conditions(
            kind=params.frequency_kind,
            center_from=params.frequency_center_from,
            center_to=params.frequency_center_to,
            bandwidth_from=params.bandwidth_from,
            bandwidth_to=params.bandwidth_to,
        )

    @classmethod
    def _sort_expression(cls, name: str, params: ObservationCriteria):
        if name == "frequency_center":
            return ObservationFrequencyFilter.lowest_center(cls._band_conditions(params))
        return super()._sort_expression(name, params)

    @classmethod
    def _apply_frequency_filters(cls, query: Select, params: ObservationCriteria) -> Select:
        """Every band condition goes into one subquery, so they must hold for the same active band"""
        conditions = cls._band_conditions(params)
        if conditions:
            query = query.where(ObservationFrequencyFilter.observation_has_band(conditions))
        return query

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: ObservationCriteria,
        access_post_ids: Optional[Iterable[int]] = None,
        archived_post_ids: Optional[FrozenSet[int]] = None,
        unit_condition: Optional[ColumnElement] = None,
    ) -> Select:
        """Apply all filters from ObservationCriteria. access_post_ids: the posts listed (None: every post);
        archived_post_ids: with include_deleted, the posts whose archived observations are shown (None: every post)"""
        params = cls.criteria(params)

        # Soft delete: archived rows only with include_deleted, at archived_post_ids
        query = cls._apply_soft_delete(query, params, Observation.post_id, archived_post_ids)

        # Post scope
        if access_post_ids is not None:
            query = query.where(Observation.post_id.in_(list(access_post_ids)))
        if unit_condition is not None:   # corps_id / brigade_id with placement (services/units.py)
            query = query.where(unit_condition)

        # === ID Filters ===
        query = cls._apply_exact(query, Observation.id, params.id)
        query = cls._apply_exact(query, Observation.user_id, params.user_id)
        query = cls._apply_exact(query, Observation.post_id, params.post_id)
        query = cls._apply_exact(query, Observation.signal_type_id, params.signal_type_id)

        # === Status Filters ===
        query = cls._apply_exact(query, Observation.status, params.status)
        if params.status_in:
            status_list = params.status_in
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

        # === Tags: any or all of them (tag_match) ===
        if params.tag_ids:
            query = query.where(ObservationTagFilter.observation_has_tags(params.tag_ids, params.tag_match))

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
        params: ObservationCriteria,
        access_post_ids: Optional[Iterable[int]] = None,
        archived_post_ids: Optional[FrozenSet[int]] = None,
        unit_condition: Optional[ColumnElement] = None,
    ) -> Select:
        """Apply filters without pagination (for counting)"""
        params = cls.criteria(params)
        # Same as apply_filters but without pagination and eager loading
        query = cls._apply_soft_delete(query, params, Observation.post_id, archived_post_ids)

        if access_post_ids is not None:
            query = query.where(Observation.post_id.in_(list(access_post_ids)))
        if unit_condition is not None:   # corps_id / brigade_id with placement (services/units.py)
            query = query.where(unit_condition)

        query = cls._apply_exact(query, Observation.id, params.id)
        query = cls._apply_exact(query, Observation.user_id, params.user_id)
        query = cls._apply_exact(query, Observation.post_id, params.post_id)
        query = cls._apply_exact(query, Observation.signal_type_id, params.signal_type_id)
        query = cls._apply_exact(query, Observation.status, params.status)

        if params.status_in:
            status_list = params.status_in
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
            query = query.where(ObservationTagFilter.observation_has_tags(params.tag_ids, params.tag_match))

        return query
