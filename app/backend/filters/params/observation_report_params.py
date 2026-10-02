"""
ObservationReportParams - Filter parameters for observation Excel reports.
Used as POST body - supports list-based filters for flexible report generation.
"""

from enum import Enum
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field


class ObservationReportType(str, Enum):
    """
    Selects report layout.

    One row per frequency band.
    RC_FREQ_PART    - rc bands only: frequency_min, frequency_max
    VIDEO_FREQ_PART - video bands only: frequency_min, frequency_max
    ALL_FIELDS      - every band with all fields: user, post, kind, emitter group, frequencies, bandwidth, duration, tags
    """
    RC_FREQ_PART    = "rc_freq_part"
    VIDEO_FREQ_PART = "video_freq_part"
    ALL_FIELDS      = "all_fields"

    @property
    def label(self) -> str:
        _labels = {
            "rc_freq_part":    "RC частоти",
            "video_freq_part": "Вiдео частоти",
            "all_fields":      "Повний звiт (всi поля)",
        }
        return _labels[self.value]

    @property
    def description(self) -> str:
        _desc = {
            "rc_freq_part":    "Частота мiн/макс, тип сигналу, коментар",
            "video_freq_part": "Вiдео частота мiн/макс, тип сигналу, коментар",
            "all_fields":      "Всi поля: користувач, пост, тип частоти, група, частоти та смуга, тривалiсть, теги",
        }
        return _desc[self.value]

    @classmethod
    def choices(cls) -> list:
        """Return list of {value, label, description} dicts for web UI select/dropdown."""
        return [
            {"value": m.value, "label": m.label, "description": m.description}
            for m in cls
        ]


class ObservationReportParams(BaseModel):
    """
    Filter parameters for generating an observation Excel report.

    Supports filtering by:
    - Specific observation IDs
    - User IDs (one or many)
    - Post IDs (one or many)
    - Signal type IDs
    - Statuses
    - Observed-at date range
    - Created-at date range
    - Any combination of the above
    """

    # --- Observation ID filter ---
    observation_ids: Optional[List[int]] = Field(
        default=None,
        description="Specific observation IDs to include in report"
    )

    # --- User filter ---
    user_ids: Optional[List[int]] = Field(
        default=None,
        description="Include only observations by these user IDs"
    )

    # --- Post filter ---
    post_ids: Optional[List[int]] = Field(
        default=None,
        description="Include only observations from these post IDs"
    )

    # --- Signal type filter ---
    signal_type_ids: Optional[List[int]] = Field(
        default=None,
        description="Include only observations of these signal type IDs"
    )

    # --- Status filter ---
    statuses: Optional[List[str]] = Field(
        default=None,
        description="Filter by statuses: active, disappeared, confirmed, false_positive, under_review"
    )

    # --- Confidence level filter ---
    confidence_level: Optional[str] = Field(
        default=None,
        description="Filter by confidence level: low, medium, high"
    )

    # --- Band range (applies to the bands of the report's kind; every kind for all_fields) ---
    frequency_center_from: Optional[Decimal] = Field(default=None, description="Band center frequency >= (MHz)")
    frequency_center_to: Optional[Decimal] = Field(default=None, description="Band center frequency <= (MHz)")
    bandwidth_from: Optional[Decimal] = Field(default=None, description="Bandwidth >= (MHz)")
    bandwidth_to: Optional[Decimal] = Field(default=None, description="Bandwidth <= (MHz)")

    # --- Duration range ---
    duration_from: Optional[int] = Field(default=None, ge=0, description="Duration >= (seconds)")
    duration_to: Optional[int] = Field(default=None, ge=0, description="Duration <= (seconds)")

    # --- Tag filter ---
    tag_ids: Optional[List[int]] = Field(default=None, description="Include only observations that have ALL of these tag IDs")

    # --- Date range (observed_at) ---
    observed_at_from: Optional[datetime] = Field(
        default=None,
        description="Include observations with observed_at >= this value (ISO datetime, UTC)"
    )
    observed_at_to: Optional[datetime] = Field(
        default=None,
        description="Include observations with observed_at <= this value (ISO datetime, UTC)"
    )

    # --- Date range (created_at) ---
    created_at_from: Optional[datetime] = Field(
        default=None,
        description="Include observations with created_at >= this value (ISO datetime, UTC)"
    )
    created_at_to: Optional[datetime] = Field(
        default=None,
        description="Include observations with created_at <= this value (ISO datetime, UTC)"
    )

    # --- Report type (required) ---
    report_type: ObservationReportType = Field(
        description="'rc_freq_part' - rc bands; 'video_freq_part' - video bands; 'all_fields' - every band, all fields"
    )

    # --- Soft delete ---
    include_deleted: bool = Field(
        default=False,
        description="Include archived observations and archived bands in the report (requires observation.archive)"
    )