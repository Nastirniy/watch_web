from typing import Optional, List
from datetime import datetime, timezone
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator

from app.backend.enums.general import ConfidenceLevel
from app.backend.utils.validators import SecureBaseModel, SecurityValidators


class ObservationCreate(BaseModel):
    """Create observation with proper validation matching DB constraints"""

    post_id: int = Field(gt=0, description="Post ID must be positive")
    signal_type_id: int = Field(gt=0, description="Signal Type ID must be positive")

    # Frequency fields with DB constraints: decimal_places=3, max_digits=12
    frequency_min: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Minimum frequency in MHz"
    )
    frequency_max: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Maximum frequency in MHz"
    )
    frequency_center: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Center frequency in MHz"
    )

    # Video frequency fields with same constraints
    video_frequency_min: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Minimum video frequency in MHz"
    )
    video_frequency_max: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Maximum video frequency in MHz"
    )
    video_frequency_center: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Center video frequency in MHz"
    )

    observed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = Field(None, ge=0, description="Duration must be non-negative")
    comment: Optional[str] = Field(None, max_length=1000, description="Comment limited to 1000 characters")
    confidence_level: Optional[str] = Field(
        None,
        max_length=10,
        description="Confidence level: low, medium, or high"
    )
    tag_ids: Optional[List[int]] = Field(
        None,
        description="List of tag IDs to associate with the observation (duplicates removed automatically)"
    )

    @field_validator('tag_ids')
    @classmethod
    def validate_tag_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        """Validate and deduplicate tag IDs"""
        if v is not None:
            for tag_id in v:
                if tag_id <= 0:
                    raise ValueError("Tag ID must be positive")
            return list(dict.fromkeys(v))
        return v

    @field_validator('confidence_level')
    @classmethod
    def validate_confidence_level(cls, v: Optional[str]) -> Optional[str]:
        """Validate confidence level against enum values"""
        if v is not None:
            valid_levels = [level.value for level in ConfidenceLevel]
            if v not in valid_levels:
                raise ValueError(f"confidence_level must be one of: {', '.join(valid_levels)}")
        return v

    @field_validator('observed_at')
    @classmethod
    def validate_observed_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Ensure observed_at has timezone info"""
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator('frequency_min', 'frequency_max', 'frequency_center', 'video_frequency_min', 'video_frequency_max', 'video_frequency_center')
    @classmethod
    def validate_frequencies(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        """Validate frequency values are positive"""
        if v is not None and v <= 0:
            raise ValueError("Frequency values must be positive")
        return v

    @model_validator(mode='after')
    def validate_frequency_pairs(self):
        """Validate that frequencies are provided in complete pairs"""
        has_default_freq = self.frequency_min is not None and self.frequency_max is not None
        has_video_freq = self.video_frequency_min is not None and self.video_frequency_max is not None

        if not has_default_freq and not has_video_freq:
            raise ValueError(
                "At least one complete frequency set must be provided: "
                "either (frequency_min, frequency_max) or (video_frequency_min, video_frequency_max)"
            )

        if (self.frequency_min is not None) != (self.frequency_max is not None):
            raise ValueError("Both frequency_min and frequency_max must be provided together")

        if (self.video_frequency_min is not None) != (self.video_frequency_max is not None):
            raise ValueError("Both video_frequency_min and video_frequency_max must be provided together")

        if has_default_freq and self.frequency_min >= self.frequency_max:
            raise ValueError("frequency_max must be greater than frequency_min")

        if has_video_freq and self.video_frequency_min >= self.video_frequency_max:
            raise ValueError("video_frequency_max must be greater than video_frequency_min")

        return self


class ObservationUpdate(BaseModel):
    is_deleted: Optional[bool] = Field(None, description="false restores a soft-deleted observation, true soft-deletes it (requires observation.delete)")
    """Update observation with proper validation matching DB constraints"""

    signal_type_id: Optional[int] = Field(None, gt=0, description="Signal Type ID must be positive")

    # Frequency fields with DB constraints: decimal_places=3, max_digits=12
    frequency_min: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Minimum frequency in MHz"
    )
    frequency_max: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Maximum frequency in MHz"
    )
    frequency_center: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Center frequency in MHz"
    )

    # Video frequency fields with same constraints
    video_frequency_min: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Minimum video frequency in MHz"
    )
    video_frequency_max: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Maximum video frequency in MHz"
    )
    video_frequency_center: Optional[Decimal] = Field(
        None,
        max_digits=12,
        decimal_places=3,
        description="Center video frequency in MHz"
    )

    observed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = Field(None, ge=0, description="Duration must be non-negative")
    comment: Optional[str] = Field(None, max_length=1000, description="Comment limited to 1000 characters")
    confidence_level: Optional[str] = Field(
        None,
        max_length=10,
        description="Confidence level: low, medium, or high"
    )
    tag_ids: Optional[List[int]] = Field(
        None,
        description="List of tag IDs to set on the observation (replaces existing tags, duplicates removed automatically)"
    )

    @field_validator('tag_ids')
    @classmethod
    def validate_tag_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        """Validate and deduplicate tag IDs"""
        if v is not None:
            for tag_id in v:
                if tag_id <= 0:
                    raise ValueError("Tag ID must be positive")
            return list(dict.fromkeys(v))
        return v

    @field_validator('confidence_level')
    @classmethod
    def validate_confidence_level(cls, v: Optional[str]) -> Optional[str]:
        """Validate confidence level against enum values"""
        if v is not None:
            valid_levels = [level.value for level in ConfidenceLevel]
            if v not in valid_levels:
                raise ValueError(f"confidence_level must be one of: {', '.join(valid_levels)}")
        return v

    @field_validator('observed_at')
    @classmethod
    def validate_observed_at(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Ensure observed_at has timezone info"""
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator('frequency_min', 'frequency_max', 'frequency_center', 'video_frequency_min', 'video_frequency_max', 'video_frequency_center')
    @classmethod
    def validate_frequencies(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        """Validate frequency values are positive"""
        if v is not None and v <= 0:
            raise ValueError("Frequency values must be positive")
        return v

    @model_validator(mode='after')
    def validate_frequency_pairs(self):
        """Validate that frequencies are provided in complete pairs during update"""
        if (self.frequency_min is not None) != (self.frequency_max is not None):
            raise ValueError("Both frequency_min and frequency_max must be provided together")

        if (self.video_frequency_min is not None) != (self.video_frequency_max is not None):
            raise ValueError("Both video_frequency_min and video_frequency_max must be provided together")

        if self.frequency_min is not None and self.frequency_max is not None:
            if self.frequency_min >= self.frequency_max:
                raise ValueError("frequency_max must be greater than frequency_min")

        if self.video_frequency_min is not None and self.video_frequency_max is not None:
            if self.video_frequency_min >= self.video_frequency_max:
                raise ValueError("video_frequency_max must be greater than video_frequency_min")

        return self


class ObservationDisappeared(BaseModel):
    """Mark observation as disappeared with validation"""

    disappeared_at: datetime = Field(description="Timestamp when observation disappeared")

    @field_validator('disappeared_at')
    @classmethod
    def validate_disappeared_at(cls, v: datetime) -> datetime:
        """Ensure disappeared_at has timezone info"""
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class ObservationFilterParams(SecureBaseModel):
    """
    Comprehensive filter parameters for observations list endpoint.
    Supports multiple filtering strategies with security validations.
    """

    # Basic filters
    post_id: Optional[int] = Field(None, gt=0, description="Filter by specific post")
    status: Optional[str] = Field(None, max_length=50, description="Filter by observation status")

    # Absolute date range filters
    observed_at_from: Optional[datetime] = Field(None, description="Filter observations on or after this datetime")
    observed_at_to: Optional[datetime] = Field(None, description="Filter observations on or before this datetime")

    # Relative date filters
    last_n_days: Optional[int] = Field(
        None,
        ge=1,
        le=365,
        description="Get observations from the last N days (counting from today backwards)"
    )
    day_offset: Optional[int] = Field(
        None,
        ge=0,
        le=365,
        description="Get observations from specific day (0=today, 1=yesterday, 2=day before, etc.)"
    )
    skip_empty_days: bool = Field(
        default=False,
        description="Skip days with no observations in relative date queries"
    )

    # Tag filter
    tag_ids: Optional[List[int]] = Field(
        None,
        description="Filter by tag IDs. Returns observations that have ALL specified tags."
    )

    # Pagination
    skip: Optional[int] = Field(None, ge=0, le=100000, description="Number of records to skip")
    limit: Optional[int] = Field(None, ge=1, le=1000, description="Maximum number of records to return")

    @field_validator('status')
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize status string"""
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=50)
        return v

    @field_validator('observed_at_from', 'observed_at_to')
    @classmethod
    def validate_datetime(cls, v: Optional[datetime]) -> Optional[datetime]:
        """Ensure datetime has timezone info"""
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator('last_n_days', 'day_offset')
    @classmethod
    def validate_day_params(cls, v: Optional[int]) -> Optional[int]:
        """Validate day parameters"""
        if v is not None:
            return SecurityValidators.validate_integer_range(v, 0, 365)
        return v

    @field_validator('skip', 'limit')
    @classmethod
    def validate_pagination(cls, v: Optional[int]) -> Optional[int]:
        """Validate pagination parameters"""
        if v is not None:
            if v < 0:
                raise ValueError("Pagination values must be non-negative")
        return v

    @model_validator(mode='after')
    def validate_date_conflicts(self):
        """Ensure only one date filtering strategy is used"""
        has_absolute_dates = self.observed_at_from is not None or self.observed_at_to is not None
        has_relative_days = self.last_n_days is not None or self.day_offset is not None

        if has_absolute_dates and has_relative_days:
            raise ValueError(
                "Cannot use both absolute date filters (observed_at_from/to) "
                "and relative date filters (last_n_days/day_offset) simultaneously"
            )

        if self.last_n_days is not None and self.day_offset is not None:
            raise ValueError("Cannot use both last_n_days and day_offset simultaneously")

        # Validate absolute date range
        if self.observed_at_from and self.observed_at_to:
            if self.observed_at_from > self.observed_at_to:
                raise ValueError("observed_at_from must be before or equal to observed_at_to")

        return self
