from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from decimal import Decimal
from sqlmodel import Field, SQLModel, Relationship
from app.backend.enums.general import ObservationStatus, ConfidenceLevel

if TYPE_CHECKING:
    from app.backend.models.user import User
    from app.backend.models.post import Post
    from app.backend.models.media import MediaFile


class Category(SQLModel, table=True):
    __tablename__ = "categories"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255, index=True)
    alias: str = Field(max_length=255)

    # Relationships
    signal_types: List["SignalType"] = Relationship(back_populates="category")


class SignalType(SQLModel, table=True):
    __tablename__ = "signal_types"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(unique=True, index=True, max_length=50)
    name: str = Field(max_length=255)
    category_id: Optional[int] = Field(default=None, foreign_key="categories.id", index=True)
    description: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, index=True)
    sort_order: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    category: Optional[Category] = Relationship(back_populates="signal_types")
    observations: List["Observation"] = Relationship(back_populates="signal_type")


class Observation(SQLModel, table=True):
    __tablename__ = "observations"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    post_id: int = Field(foreign_key="posts.id", index=True)
    signal_type_id: int = Field(foreign_key="signal_types.id", index=True)

    frequency_min: Decimal = Field(decimal_places=3, max_digits=12)
    frequency_max: Decimal = Field(decimal_places=3, max_digits=12)
    frequency_center: Optional[Decimal] = Field(default=None, decimal_places=3, max_digits=12, index=True)
    bandwidth: Optional[Decimal] = Field(default=None, decimal_places=3, max_digits=12)

    video_frequency_min: Optional[Decimal] = Field(default=None, decimal_places=3, max_digits=12)
    video_frequency_max: Optional[Decimal] = Field(default=None, decimal_places=3, max_digits=12)
    video_frequency_center: Optional[Decimal] = Field(default=None, decimal_places=3, max_digits=12, index=True)
    video_bandwidth: Optional[Decimal] = Field(default=None, decimal_places=3, max_digits=12)

    observed_at: datetime = Field(index=True)
    duration_seconds: Optional[int] = Field(default=None)

    comment: Optional[str] = Field(default=None, max_length=1000)

    status: str = Field(default=ObservationStatus.ACTIVE.value, index=True, max_length=20)
    confidence_level: str = Field(default=ConfidenceLevel.MEDIUM.value, max_length=10)
    is_deleted: bool = Field(default=False)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    disappeared_at: Optional[datetime] = Field(default=None, index=True)
    disappeared_by: Optional[int] = Field(default=None, foreign_key="users.id")

    # Relationships
    user: "User" = Relationship(
        back_populates="observations",
        sa_relationship_kwargs={"foreign_keys": "[Observation.user_id]"}
    )
    post: "Post" = Relationship(back_populates="observations")
    signal_type: SignalType = Relationship(back_populates="observations")
    disappeared_by_user: Optional["User"] = Relationship(
        back_populates="disappeared_observations",
        sa_relationship_kwargs={"foreign_keys": "[Observation.disappeared_by]"}
    )
    media_files: List["MediaFile"] = Relationship(back_populates="observation", cascade_delete=True)
