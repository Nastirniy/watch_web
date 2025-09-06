from typing import Optional, List
from datetime import datetime, timezone
from decimal import Decimal
from sqlmodel import Field, SQLModel, Relationship, JSON, Column
from sqlalchemy import LargeBinary
from app.backend.enums.general import ObservationStatus, ConfidenceLevel, MediaType


class Category(SQLModel, table=True):
    __tablename__ = "categories"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255, index=True)
    alias: str = Field(max_length=255)

    # Relationships
    signal_types: List["SignalType"] = Relationship(back_populates="category")


class ModulationType(SQLModel, table=True):
    __tablename__ = "modulation_types"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)

    # Relationships
    observations: List["Observation"] = Relationship(back_populates="modulation_type")


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

    observed_at: datetime = Field(index=True)
    duration_seconds: Optional[int] = Field(default=None)

    signal_strength: Optional[Decimal] = Field(default=None, decimal_places=2, max_digits=8)
    modulation_type_id: Optional[int] = Field(default=None, foreign_key="modulation_types.id")

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
    modulation_type: Optional[ModulationType] = Relationship(back_populates="observations")
    disappeared_by_user: Optional["User"] = Relationship(
        back_populates="disappeared_observations",
        sa_relationship_kwargs={"foreign_keys": "[Observation.disappeared_by]"}
    )
    media_files: List["MediaFile"] = Relationship(back_populates="observation", cascade_delete=True)


class MediaFile(SQLModel, table=True):
    __tablename__ = "media_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    observation_id: int = Field(foreign_key="observations.id", index=True)

    original_filename: str = Field(max_length=255)
    stored_filename: str = Field(unique=True, max_length=255)
    file_path: str = Field(max_length=500)
    file_size: int
    mime_type: str = Field(max_length=100)
    file_extension: str = Field(max_length=10)

    media_type: str = Field(max_length=10, index=True)

    file_hash: str = Field(max_length=64, index=True)

    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)

    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

    # Relationships
    observation: Observation = Relationship(back_populates="media_files")
    file_chunks: List["FileChunk"] = Relationship(back_populates="media_file", cascade_delete=True)


class FileChunk(SQLModel, table=True):
    __tablename__ = "file_chunks"

    id: Optional[int] = Field(default=None, primary_key=True)
    media_file_id: int = Field(foreign_key="media_files.id", index=True)
    chunk_number: int = Field(index=True)
    chunk_size: int
    chunk_hash: str = Field(max_length=64)
    chunk_data: bytes = Field(sa_column=Column(LargeBinary))
    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    media_file: MediaFile = Relationship(back_populates="file_chunks")