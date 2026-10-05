"""
ObservationReportFilter - Query filter for observation Excel reports.
One report row per frequency band; the query selects bands joined with their
observation (ObservationFrequencyFilter.rows_query), so archive flags and the
post scope always apply. No pagination - fetches all matching rows for export.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, List, Optional

from sqlalchemy.orm import contains_eager
from sqlalchemy.sql.selectable import Select
from sqlmodel import select as sa_select

from app.backend.enums.general import FrequencyKind, ObservationReportType
from app.backend.models.observation import Observation, ObservationFrequency, ObservationTag
from app.backend.filters.observation_frequency_filter import ObservationFrequencyFilter
from app.backend.filters.simple_engine import as_criteria

REPORT_TYPE_KIND = {
    ObservationReportType.RC_FREQ_PART: FrequencyKind.RC,
    ObservationReportType.VIDEO_FREQ_PART: FrequencyKind.VIDEO,
}


@dataclass(frozen=True, kw_only=True)
class ObservationReportCriteria:
    """What the report filters by; each API version converts its request body into it"""
    report_type: ObservationReportType
    observation_ids: Optional[List[int]] = None
    user_ids: Optional[List[int]] = None
    post_ids: Optional[List[int]] = None
    signal_type_ids: Optional[List[int]] = None
    statuses: Optional[List[str]] = None
    confidence_level: Optional[str] = None
    frequency_center_from: Optional[Decimal] = None
    frequency_center_to: Optional[Decimal] = None
    bandwidth_from: Optional[Decimal] = None
    bandwidth_to: Optional[Decimal] = None
    duration_from: Optional[int] = None
    duration_to: Optional[int] = None
    tag_ids: Optional[List[int]] = None
    observed_at_from: Optional[datetime] = None
    observed_at_to: Optional[datetime] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None
    include_deleted: bool = False


class ObservationReportFilter:
    """
    Filter engine for observation report generation.
    Supports list-based filters (IDs, users, posts) and date ranges.
    No pagination - returns all matched bands for the report.
    """

    MODEL = ObservationFrequency

    @classmethod
    def build_query(cls, params: ObservationReportCriteria, access_post_ids: Optional[Iterable[int]] = None) -> Select:
        """Bands matching the report filters, their observation and its report relations eager-loaded."""
        params = as_criteria(ObservationReportCriteria, params)
        query = ObservationFrequencyFilter.rows_query(access_post_ids, include_deleted=params.include_deleted)

        # Band filters: the report type selects the kind
        query = query.where(*ObservationFrequencyFilter.band_conditions(
            kind=REPORT_TYPE_KIND.get(params.report_type),
            center_from=params.frequency_center_from,
            center_to=params.frequency_center_to,
            bandwidth_from=params.bandwidth_from,
            bandwidth_to=params.bandwidth_to,
        ))

        # Observation ID filter
        if params.observation_ids:
            query = query.where(Observation.id.in_(params.observation_ids))

        # User filter
        if params.user_ids:
            query = query.where(Observation.user_id.in_(params.user_ids))

        # Post filter
        if params.post_ids:
            query = query.where(Observation.post_id.in_(params.post_ids))

        # Signal type filter
        if params.signal_type_ids:
            query = query.where(Observation.signal_type_id.in_(params.signal_type_ids))

        # Status filter
        if params.statuses:
            query = query.where(Observation.status.in_(params.statuses))

        # Confidence level filter
        if params.confidence_level:
            query = query.where(Observation.confidence_level == params.confidence_level)

        # Duration range
        if params.duration_from is not None:
            query = query.where(Observation.duration_seconds >= params.duration_from)
        if params.duration_to is not None:
            query = query.where(Observation.duration_seconds <= params.duration_to)

        # Tag filter (observation must have ANY of the requested tags)
        if params.tag_ids:
            query = query.where(
                Observation.id.in_(
                    sa_select(ObservationTag.observation_id).where(ObservationTag.tag_id.in_(params.tag_ids))
                )
            )

        # Date range (observed_at)
        if params.observed_at_from is not None:
            query = query.where(Observation.observed_at >= params.observed_at_from)
        if params.observed_at_to is not None:
            query = query.where(Observation.observed_at <= params.observed_at_to)

        # Date range (created_at)
        if params.created_at_from is not None:
            query = query.where(Observation.created_at >= params.created_at_from)
        if params.created_at_to is not None:
            query = query.where(Observation.created_at <= params.created_at_to)

        # Default sort: by observation time ascending, bands of one observation together
        query = query.order_by(Observation.observed_at.asc(), Observation.id.asc(), ObservationFrequency.id.asc())

        # The joined observation fills ObservationFrequency.observation; its relations load in one query each
        observation = contains_eager(ObservationFrequency.observation)
        query = query.options(
            observation.selectinload(Observation.user),
            observation.selectinload(Observation.post),
            observation.selectinload(Observation.signal_type),
            observation.selectinload(Observation.tags),
        )

        return query