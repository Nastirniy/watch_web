"""
ObservationFilterParams - Strongly typed filter parameters for Observation
All fields visible and documented in FastAPI /docs
"""

from typing import Optional, List, Literal
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field

from app.backend.enums.general import FrequencyKind, TagMatch
from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
ObservationSortField = Literal["id", "observed_at", "created_at", "updated_at", "status", "post_id", "frequency_center"]


class ObservationFilterParams(BaseFilterParams):
    """Filter parameters for Observation search"""

    # --- ID Filters ---
    sort_by: Optional[ObservationSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact observation ID")
    user_id: Optional[int] = Field(default=None, description="Filter by user ID")
    post_id: Optional[int] = Field(default=None, description="Filter by post ID")
    signal_type_id: Optional[int] = Field(default=None, description="Filter by signal type ID")

    # --- Status ---
    status: Optional[str] = Field(default=None, description="Exact status (active, disappeared, confirmed, false_positive, under_review)")
    status_in: Optional[List[str]] = Field(default=None, description="Any of these statuses: ?status_in=active&status_in=confirmed")
    confidence_level: Optional[str] = Field(default=None, description="Confidence level (low, medium, high)")

    # --- Frequency bands (all conditions must hold for the same active band) ---
    frequency_kind: Optional[FrequencyKind] = Field(default=None, description="Band kind: rc or video")
    frequency_center_from: Optional[Decimal] = Field(default=None, description="Band center frequency min (MHz)")
    frequency_center_to: Optional[Decimal] = Field(default=None, description="Band center frequency max (MHz)")
    bandwidth_from: Optional[Decimal] = Field(default=None, description="Bandwidth min (MHz)")
    bandwidth_to: Optional[Decimal] = Field(default=None, description="Bandwidth max (MHz)")

    # --- Date Range (observed_at) ---
    observed_at_from: Optional[datetime] = Field(default=None, description="Observed after (ISO datetime)")
    observed_at_to: Optional[datetime] = Field(default=None, description="Observed before (ISO datetime)")

    # --- Relative Date (alternative to observed_at range) ---
    last_n_days: Optional[int] = Field(default=None, ge=1, le=365, description="Last N days from today")

    # --- Created/Updated Range ---
    created_at_from: Optional[datetime] = Field(default=None, description="Created after")
    created_at_to: Optional[datetime] = Field(default=None, description="Created before")

    # --- Disappeared ---
    is_disappeared: Optional[bool] = Field(default=None, description="True=disappeared, False=not disappeared")
    disappeared_by: Optional[int] = Field(default=None, description="User ID who marked disappeared")

    # --- Duration ---
    duration_from: Optional[int] = Field(default=None, ge=0, description="Duration min (seconds)")
    duration_to: Optional[int] = Field(default=None, ge=0, description="Duration max (seconds)")

    # --- Text Search ---
    comment_contains: Optional[str] = Field(default=None, max_length=100, description="Comment contains text")

    # --- Tags ---
    tag_ids: Optional[List[int]] = Field(default=None, description="Tag ids: ?tag_ids=1&tag_ids=2; tag_match says whether any or all of them")
    tag_match: TagMatch = Field(default=TagMatch.ANY, description="any: observations with at least one of tag_ids; all: with every one of them")

    # --- Soft Delete ---
    include_deleted: bool = Field(default=False, description="Include archived records (requires observation.archive)")


class ObservationFrequencyListParams(BaseModel):
    """Query parameters of GET /observations/{observation_id}/frequencies"""

    include_deleted: bool = Field(
        default=False,
        description="Also archived bands, and bands of an archived observation (requires observation.archive)"
    )
