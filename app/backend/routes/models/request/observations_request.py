from typing import Optional
from datetime import datetime, timezone
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator

from app.backend.enums.general import ConfidenceLevel


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

    observed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = Field(None, ge=0, description="Duration must be non-negative")
    comment: Optional[str] = Field(None, max_length=1000, description="Comment limited to 1000 characters")
    confidence_level: Optional[str] = Field(
        None,
        max_length=10,
        description="Confidence level: low, medium, or high"
    )

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

    @field_validator('frequency_min', 'frequency_max', 'video_frequency_min', 'video_frequency_max')
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

    observed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = Field(None, ge=0, description="Duration must be non-negative")
    comment: Optional[str] = Field(None, max_length=1000, description="Comment limited to 1000 characters")
    confidence_level: Optional[str] = Field(
        None,
        max_length=10,
        description="Confidence level: low, medium, or high"
    )

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

    @field_validator('frequency_min', 'frequency_max', 'video_frequency_min', 'video_frequency_max')
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
