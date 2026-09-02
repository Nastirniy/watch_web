"""
ObservationReportFilter - Query filter for observation Excel reports.
No pagination - fetches all matching records for export.
"""

from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Select
from sqlmodel import select as sa_select

from app.backend.models.observation import Observation, ObservationTag
from app.backend.filters.params.observation_report_params import ObservationReportParams, ObservationReportType


class ObservationReportFilter:
    """
    Filter engine for observation report generation.
    Supports list-based filters (IDs, users, posts) and date ranges.
    No pagination - returns all matched records for the report.
    """

    MODEL = Observation

    @classmethod
    def apply_filters(cls, query: Select, params: ObservationReportParams) -> Select:
        """Apply report filters to the base query. Eager-loads all report relations."""

        # Soft delete
        if not params.include_deleted:
            query = query.where(Observation.is_deleted == False)

        # Report type row filter: only include rows that have the relevant frequency data
        if params.report_type == ObservationReportType.RC_FREQ_PART:
            query = query.where(
                Observation.frequency_min.is_not(None),
                Observation.frequency_max.is_not(None),
            )
        elif params.report_type == ObservationReportType.VIDEO_FREQ_PART:
            query = query.where(
                Observation.video_frequency_min.is_not(None),
                Observation.video_frequency_max.is_not(None),
            )

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

        # RC frequency range
        if params.frequency_center_from is not None:
            query = query.where(Observation.frequency_center >= params.frequency_center_from)
        if params.frequency_center_to is not None:
            query = query.where(Observation.frequency_center <= params.frequency_center_to)
        if params.bandwidth_from is not None:
            query = query.where(Observation.bandwidth >= params.bandwidth_from)
        if params.bandwidth_to is not None:
            query = query.where(Observation.bandwidth <= params.bandwidth_to)

        # Video frequency range
        if params.video_frequency_center_from is not None:
            query = query.where(Observation.video_frequency_center >= params.video_frequency_center_from)
        if params.video_frequency_center_to is not None:
            query = query.where(Observation.video_frequency_center <= params.video_frequency_center_to)

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

        # Default sort: by observation time ascending
        query = query.order_by(Observation.observed_at.asc())

        # Eager-load all relations used in the report to avoid N+1 queries
        query = query.options(
            selectinload(Observation.user),
            selectinload(Observation.post),
            selectinload(Observation.signal_type),
            selectinload(Observation.tags),
        )

        return query