"""
Signal monitoring schemas for observations, signal types, and analysis.
Handles signal detection data and classification systems.
"""

from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, field_validator

from backend.database.models import ObservationStatus, ConfidenceLevel
from backend.api.schemas.base import BaseSchema, TimestampSchema, SearchFilters, DateRangeFilter


class CategoryBase(BaseSchema):
    """Base category schema."""

    name: str = Field(description="Category name")
    alias: str = Field(description="Category alias or short name")
    description: Optional[str] = Field(None, description="Category description")
    color_code: Optional[str] = Field(None, description="Hex color code for UI")


class CategoryCreate(CategoryBase):
    """Schema for creating categories."""

    @field_validator('color_code')
    @classmethod
    def validate_color_code(cls, v: Optional[str]) -> Optional[str]:
        if v and not v.startswith('#') or len(v) != 7:
            raise ValueError("Color code must be in hex format (#RRGGBB)")
        return v


class CategoryUpdate(BaseSchema):
    """Schema for updating categories."""

    name: Optional[str] = Field(None, description="Category name")
    alias: Optional[str] = Field(None, description="Category alias")
    description: Optional[str] = Field(None, description="Category description")
    color_code: Optional[str] = Field(None, description="Hex color code")


class Category(CategoryBase, TimestampSchema):
    """Category response schema."""

    id: int = Field(description="Category ID")
    signal_type_count: Optional[int] = Field(None, description="Number of signal types in category")


class ModulationTypeBase(BaseSchema):
    """Base modulation type schema."""

    name: str = Field(description="Modulation type name")
    abbreviation: Optional[str] = Field(None, description="Short abbreviation")
    description: Optional[str] = Field(None, description="Modulation type description")
    bandwidth_typical: Optional[float] = Field(None, description="Typical bandwidth in kHz")
    symbol_rate: Optional[float] = Field(None, description="Symbol rate in symbols/sec")


class ModulationTypeCreate(ModulationTypeBase):
    """Schema for creating modulation types."""
    pass


class ModulationTypeUpdate(BaseSchema):
    """Schema for updating modulation types."""

    name: Optional[str] = Field(None, description="Modulation type name")
    abbreviation: Optional[str] = Field(None, description="Short abbreviation")
    description: Optional[str] = Field(None, description="Description")
    bandwidth_typical: Optional[float] = Field(None, description="Typical bandwidth in kHz")
    symbol_rate: Optional[float] = Field(None, description="Symbol rate")


class ModulationType(ModulationTypeBase, TimestampSchema):
    """Modulation type response schema."""

    id: int = Field(description="Modulation type ID")
    observation_count: Optional[int] = Field(None, description="Number of observations using this type")


class SignalTypeBase(BaseSchema):
    """Base signal type schema."""

    code: str = Field(description="Unique signal type code")
    name: str = Field(description="Signal type name")
    description: Optional[str] = Field(None, description="Signal type description")
    category_id: Optional[int] = Field(None, description="Category ID")
    frequency_min: Optional[Decimal] = Field(None, description="Minimum frequency in MHz")
    frequency_max: Optional[Decimal] = Field(None, description="Maximum frequency in MHz")
    bandwidth_typical: Optional[Decimal] = Field(None, description="Typical bandwidth in kHz")
    is_active: bool = Field(default=True, description="Active status")
    sort_order: int = Field(default=0, description="Display sort order")
    priority: int = Field(default=0, description="Signal priority level")
    auto_classification: bool = Field(default=False, description="Can be auto-classified")
    classification_threshold: Optional[float] = Field(None, description="Auto-classification threshold")


class SignalTypeCreate(SignalTypeBase):
    """Schema for creating signal types."""

    @field_validator('code')
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not v or len(v.strip()) < 2:
            raise ValueError("Signal type code must be at least 2 characters")
        return v.upper().strip()

    @field_validator('frequency_min', 'frequency_max')
    @classmethod
    def validate_frequency(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v <= 0:
            raise ValueError("Frequency must be positive")
        return v


class SignalTypeUpdate(BaseSchema):
    """Schema for updating signal types."""

    name: Optional[str] = Field(None, description="Signal type name")
    description: Optional[str] = Field(None, description="Description")
    category_id: Optional[int] = Field(None, description="Category ID")
    frequency_min: Optional[Decimal] = Field(None, description="Minimum frequency in MHz")
    frequency_max: Optional[Decimal] = Field(None, description="Maximum frequency in MHz")
    bandwidth_typical: Optional[Decimal] = Field(None, description="Typical bandwidth in kHz")
    is_active: Optional[bool] = Field(None, description="Active status")
    sort_order: Optional[int] = Field(None, description="Display sort order")
    priority: Optional[int] = Field(None, description="Signal priority level")
    auto_classification: Optional[bool] = Field(None, description="Auto-classification enabled")
    classification_threshold: Optional[float] = Field(None, description="Classification threshold")


class SignalType(SignalTypeBase, TimestampSchema):
    """Signal type response schema."""

    id: int = Field(description="Signal type ID")
    category: Optional[Category] = Field(None, description="Signal category")
    observation_count: Optional[int] = Field(None, description="Number of observations")
    last_observation_at: Optional[datetime] = Field(None, description="Last observation timestamp")


class ObservationBase(BaseSchema):
    """Base observation schema."""

    post_id: int = Field(description="Monitoring post ID")
    signal_type_id: int = Field(description="Signal type ID")
    modulation_type_id: Optional[int] = Field(None, description="Modulation type ID")
    frequency_min: Decimal = Field(description="Minimum frequency in MHz")
    frequency_max: Decimal = Field(description="Maximum frequency in MHz")
    observed_at: datetime = Field(description="Signal observation timestamp")
    duration_seconds: Optional[int] = Field(None, description="Observation duration in seconds")
    signal_strength: Optional[Decimal] = Field(None, description="Signal strength in dBm")
    snr: Optional[Decimal] = Field(None, description="Signal-to-noise ratio in dB")
    comment: Optional[str] = Field(None, max_length=1000, description="Observer comments")
    confidence_level: ConfidenceLevel = Field(default=ConfidenceLevel.MEDIUM, description="Confidence level")
    equipment_info: Optional[str] = Field(None, description="Equipment used for observation")
    weather_conditions: Optional[str] = Field(None, description="Weather conditions during observation")


class ObservationCreate(ObservationBase):
    """Schema for creating observations."""

    @field_validator('frequency_min', 'frequency_max')
    @classmethod
    def validate_frequencies(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Frequency must be positive")
        return v

    @field_validator('frequency_max')
    @classmethod
    def validate_frequency_range(cls, v: Decimal, info) -> Decimal:
        if 'frequency_min' in info.data and v <= info.data['frequency_min']:
            raise ValueError("Maximum frequency must be greater than minimum frequency")
        return v

    @field_validator('duration_seconds')
    @classmethod
    def validate_duration(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("Duration must be positive")
        return v


class ObservationUpdate(BaseSchema):
    """Schema for updating observations."""

    signal_type_id: Optional[int] = Field(None, description="Signal type ID")
    modulation_type_id: Optional[int] = Field(None, description="Modulation type ID")
    frequency_min: Optional[Decimal] = Field(None, description="Minimum frequency in MHz")
    frequency_max: Optional[Decimal] = Field(None, description="Maximum frequency in MHz")
    duration_seconds: Optional[int] = Field(None, description="Duration in seconds")
    signal_strength: Optional[Decimal] = Field(None, description="Signal strength in dBm")
    snr: Optional[Decimal] = Field(None, description="Signal-to-noise ratio in dB")
    comment: Optional[str] = Field(None, max_length=1000, description="Comments")
    confidence_level: Optional[ConfidenceLevel] = Field(None, description="Confidence level")
    equipment_info: Optional[str] = Field(None, description="Equipment information")
    weather_conditions: Optional[str] = Field(None, description="Weather conditions")


class Observation(ObservationBase, TimestampSchema):
    """Observation response schema."""

    id: int = Field(description="Observation ID")
    user_id: int = Field(description="Observer user ID")
    frequency_center: Optional[Decimal] = Field(None, description="Center frequency in MHz")
    bandwidth: Optional[Decimal] = Field(None, description="Bandwidth in kHz")
    status: ObservationStatus = Field(description="Observation status")
    disappeared_at: Optional[datetime] = Field(None, description="Disappearance timestamp")
    disappeared_by: Optional[int] = Field(None, description="User who marked as disappeared")
    is_deleted: bool = Field(description="Soft delete flag")

    # Related objects
    user: Optional["UserSummary"] = Field(None, description="Observer information")
    post: Optional["PostSummary"] = Field(None, description="Monitoring post information")
    signal_type: Optional[SignalType] = Field(None, description="Signal type information")
    modulation_type: Optional[ModulationType] = Field(None, description="Modulation type information")
    media_file_count: Optional[int] = Field(None, description="Number of attached media files")

    @property
    def duration_formatted(self) -> Optional[str]:
        """Get formatted duration string."""
        if self.duration_seconds is None:
            return None

        hours, remainder = divmod(self.duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"


class ObservationSummary(BaseSchema):
    """Simplified observation schema for listings."""

    id: int = Field(description="Observation ID")
    post_id: int = Field(description="Post ID")
    signal_type_id: int = Field(description="Signal type ID")
    frequency_min: Decimal = Field(description="Minimum frequency in MHz")
    frequency_max: Decimal = Field(description="Maximum frequency in MHz")
    frequency_center: Optional[Decimal] = Field(None, description="Center frequency in MHz")
    observed_at: datetime = Field(description="Observation timestamp")
    signal_strength: Optional[Decimal] = Field(None, description="Signal strength in dBm")
    status: ObservationStatus = Field(description="Status")
    confidence_level: ConfidenceLevel = Field(description="Confidence level")
    user_id: int = Field(description="Observer user ID")
    username: Optional[str] = Field(None, description="Observer username")
    post_name: Optional[str] = Field(None, description="Post name")
    signal_type_name: Optional[str] = Field(None, description="Signal type name")


class ObservationSearchFilters(SearchFilters, DateRangeFilter):
    """Observation search filters."""

    post_ids: Optional[List[int]] = Field(None, description="Filter by post IDs")
    user_ids: Optional[List[int]] = Field(None, description="Filter by user IDs")
    signal_type_ids: Optional[List[int]] = Field(None, description="Filter by signal type IDs")
    statuses: Optional[List[ObservationStatus]] = Field(None, description="Filter by statuses")
    confidence_levels: Optional[List[ConfidenceLevel]] = Field(None, description="Filter by confidence levels")
    frequency_min: Optional[Decimal] = Field(None, description="Minimum frequency filter")
    frequency_max: Optional[Decimal] = Field(None, description="Maximum frequency filter")
    signal_strength_min: Optional[Decimal] = Field(None, description="Minimum signal strength")
    signal_strength_max: Optional[Decimal] = Field(None, description="Maximum signal strength")
    include_deleted: bool = Field(False, description="Include soft-deleted observations")
    has_media: Optional[bool] = Field(None, description="Filter by media file presence")


class BulkObservationUpdate(BaseSchema):
    """Bulk observation update schema."""

    observation_ids: List[int] = Field(description="Observation IDs to update")
    status: Optional[ObservationStatus] = Field(None, description="New status")
    signal_type_id: Optional[int] = Field(None, description="New signal type ID")
    confidence_level: Optional[ConfidenceLevel] = Field(None, description="New confidence level")
    comment_append: Optional[str] = Field(None, description="Text to append to comments")


class ObservationStatistics(BaseSchema):
    """Observation statistics schema."""

    total_observations: int = Field(description="Total number of observations")
    active_observations: int = Field(description="Active observations")
    by_status: dict = Field(default_factory=dict, description="Count by status")
    by_confidence: dict = Field(default_factory=dict, description="Count by confidence level")
    by_post: dict = Field(default_factory=dict, description="Count by post")
    by_signal_type: dict = Field(default_factory=dict, description="Count by signal type")
    by_user: dict = Field(default_factory=dict, description="Count by user")
    frequency_range: dict = Field(default_factory=dict, description="Frequency range statistics")
    time_range: dict = Field(default_factory=dict, description="Time range statistics")


class FrequencyAnalysis(BaseSchema):
    """Frequency analysis result."""

    frequency_min: Decimal = Field(description="Minimum frequency")
    frequency_max: Decimal = Field(description="Maximum frequency")
    frequency_center: Decimal = Field(description="Center frequency")
    bandwidth: Decimal = Field(description="Bandwidth")
    observation_count: int = Field(description="Number of observations in this range")
    signal_types: List[str] = Field(description="Signal types found in this range")
    strength_avg: Optional[Decimal] = Field(None, description="Average signal strength")
    last_observed: datetime = Field(description="Last observation in this range")


class TrendAnalysis(BaseSchema):
    """Trend analysis result."""

    period: str = Field(description="Analysis period (daily, weekly, monthly)")
    start_date: date = Field(description="Start date")
    end_date: date = Field(description="End date")
    observation_counts: List[dict] = Field(description="Observation counts by period")
    signal_type_trends: List[dict] = Field(description="Signal type trends")
    frequency_trends: List[dict] = Field(description="Frequency usage trends")
    post_activity: List[dict] = Field(description="Post activity trends")


# Forward reference resolution
from backend.api.schemas.user import UserSummary  # noqa: E402
from backend.api.schemas.post import PostSummary  # noqa: E402

Observation.model_rebuild()
