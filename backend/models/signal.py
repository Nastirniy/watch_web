"""
Signal monitoring models including types, observations, and metadata.
Core models for signal detection and classification system.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, TYPE_CHECKING

from sqlmodel import Field, Relationship

from backend.models.base import BaseModel, BaseModelWithSoftDelete, StatusMixin

if TYPE_CHECKING:
    from backend.models.user import User
    from backend.models.post import Post
    from backend.models.media import MediaFile


class ObservationStatus(str, Enum):
    """Signal observation status enumeration."""
    ACTIVE = "active"
    DISAPPEARED = "disappeared"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    UNDER_REVIEW = "under_review"


class ConfidenceLevel(str, Enum):
    """Confidence level enumeration for observations."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Category(BaseModel, table=True):
    """Signal category classification."""

    __tablename__ = "categories"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255, unique=True, index=True, description="Category name")
    alias: str = Field(max_length=255, description="Category alias or short name")
    description: Optional[str] = Field(default=None, description="Category description")
    color_code: Optional[str] = Field(default=None, max_length=7, description="Hex color code for UI")

    # Relationships
    signal_types: List["SignalType"] = Relationship(back_populates="category")


class ModulationType(BaseModel, table=True):
    """Signal modulation type classification."""

    __tablename__ = "modulation_types"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255, unique=True, index=True, description="Modulation type name")
    abbreviation: Optional[str] = Field(default=None, max_length=10, description="Short abbreviation")
    description: Optional[str] = Field(default=None, description="Modulation type description")

    # Technical parameters
    bandwidth_typical: Optional[float] = Field(default=None, description="Typical bandwidth in kHz")
    symbol_rate: Optional[float] = Field(default=None, description="Symbol rate in symbols/sec")

    # Relationships
    observations: List["Observation"] = Relationship(back_populates="modulation_type")


class SignalType(BaseModel, StatusMixin, table=True):
    """Signal type classification and metadata."""

    __tablename__ = "signal_types"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Basic information
    code: str = Field(
        max_length=50,
        unique=True,
        index=True,
        description="Unique signal type code"
    )
    name: str = Field(max_length=255, description="Signal type name")
    description: Optional[str] = Field(default=None, description="Signal type description")

    # Classification
    category_id: Optional[int] = Field(default=None, foreign_key="categories.id", index=True)

    # Technical parameters
    frequency_min: Optional[Decimal] = Field(default=None, description="Minimum frequency in MHz")
    frequency_max: Optional[Decimal] = Field(default=None, description="Maximum frequency in MHz")
    bandwidth_typical: Optional[Decimal] = Field(default=None, description="Typical bandwidth in kHz")

    # Priority and sorting
    sort_order: int = Field(default=0, index=True, description="Display sort order")
    priority: int = Field(default=0, description="Signal priority level")

    # Classification confidence
    auto_classification: bool = Field(default=False, description="Can be auto-classified")
    classification_threshold: Optional[float] = Field(default=None, description="Auto-classification threshold")

    # Relationships
    category: Optional[Category] = Relationship(back_populates="signal_types")
    observations: List["Observation"] = Relationship(back_populates="signal_type")


class Observation(BaseModelWithSoftDelete, table=True):
    """Signal observation record with detailed measurements."""

    __tablename__ = "observations"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Foreign keys
    user_id: int = Field(foreign_key="users.id", index=True, description="Observer user ID")
    post_id: int = Field(foreign_key="posts.id", index=True, description="Monitoring post ID")
    signal_type_id: int = Field(foreign_key="signal_types.id", index=True, description="Signal type ID")
    modulation_type_id: Optional[int] = Field(
        default=None,
        foreign_key="modulation_types.id",
        description="Modulation type ID"
    )

    # Frequency measurements (in MHz)
    frequency_min: Decimal = Field(
        max_digits=12,
        decimal_places=3,
        index=True,
        description="Minimum frequency in MHz"
    )
    frequency_max: Decimal = Field(
        max_digits=12,
        decimal_places=3,
        index=True,
        description="Maximum frequency in MHz"
    )
    frequency_center: Optional[Decimal] = Field(
        default=None,
        max_digits=12,
        decimal_places=3,
        index=True,
        description="Center frequency in MHz (calculated)"
    )
    bandwidth: Optional[Decimal] = Field(
        default=None,
        max_digits=12,
        decimal_places=3,
        description="Bandwidth in kHz (calculated)"
    )

    # Observation timing
    observed_at: datetime = Field(index=True, description="Signal observation timestamp")
    duration_seconds: Optional[int] = Field(default=None, description="Observation duration in seconds")

    # Signal characteristics
    signal_strength: Optional[Decimal] = Field(
        default=None,
        max_digits=8,
        decimal_places=2,
        description="Signal strength in dBm"
    )
    snr: Optional[Decimal] = Field(
        default=None,
        max_digits=8,
        decimal_places=2,
        description="Signal-to-noise ratio in dB"
    )

    # Metadata
    comment: Optional[str] = Field(default=None, max_length=1000, description="Observer comments")
    status: ObservationStatus = Field(
        default=ObservationStatus.ACTIVE,
        index=True,
        description="Observation status"
    )
    confidence_level: ConfidenceLevel = Field(
        default=ConfidenceLevel.MEDIUM,
        description="Observer confidence level"
    )

    # Disappearance tracking
    disappeared_at: Optional[datetime] = Field(
        default=None,
        index=True,
        description="Signal disappearance timestamp"
    )
    disappeared_by: Optional[int] = Field(
        default=None,
        foreign_key="users.id",
        description="User who marked signal as disappeared"
    )

    # Technical metadata
    equipment_info: Optional[str] = Field(default=None, description="Equipment used for observation")
    weather_conditions: Optional[str] = Field(default=None, description="Weather conditions during observation")

    # Relationships
    user: "User" = Relationship(back_populates="observations")
    post: "Post" = Relationship(back_populates="observations")
    signal_type: SignalType = Relationship(back_populates="observations")
    modulation_type: Optional[ModulationType] = Relationship(back_populates="observations")
    disappeared_by_user: Optional["User"] = Relationship()
    media_files: List["MediaFile"] = Relationship(back_populates="observation", cascade_delete=True)

    def __init__(self, **data):
        """Initialize observation and calculate derived fields."""
        super().__init__(**data)
        self._calculate_derived_fields()

    def _calculate_derived_fields(self) -> None:
        """Calculate center frequency and bandwidth from min/max frequencies."""
        if self.frequency_min is not None and self.frequency_max is not None:
            # Calculate center frequency
            self.frequency_center = (self.frequency_min + self.frequency_max) / 2

            # Calculate bandwidth in kHz (frequency is in MHz)
            bandwidth_mhz = self.frequency_max - self.frequency_min
            self.bandwidth = bandwidth_mhz * 1000  # Convert MHz to kHz

    @property
    def is_active(self) -> bool:
        """Check if observation is active."""
        return self.status == ObservationStatus.ACTIVE and not self.is_deleted

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

    def mark_disappeared(self, user_id: int, comment: Optional[str] = None) -> None:
        """Mark observation as disappeared."""
        self.status = ObservationStatus.DISAPPEARED
        self.disappeared_at = datetime.utcnow()
        self.disappeared_by = user_id
        if comment:
            if self.comment:
                self.comment += f"\n[Disappeared] {comment}"
            else:
                self.comment = f"[Disappeared] {comment}"
