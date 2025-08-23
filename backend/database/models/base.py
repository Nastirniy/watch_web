"""
Base model classes with common fields and methods.
Provides timestamp tracking and soft delete functionality.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class TimestampMixin(SQLModel):
    """Mixin for created_at and updated_at timestamps."""

    created_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
        description="Record creation timestamp"
    )
    updated_at: Optional[datetime] = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={"onupdate": datetime.utcnow},
        description="Record last update timestamp"
    )


class SoftDeleteMixin(SQLModel):
    """Mixin for soft delete functionality."""

    is_deleted: bool = Field(default=False, description="Soft delete flag")


class BaseModel(TimestampMixin):
    """Base model with timestamps."""
    pass


class BaseModelWithSoftDelete(TimestampMixin, SoftDeleteMixin):
    """Base model with timestamps and soft delete."""
    pass


class StatusMixin(SQLModel):
    """Mixin for entities with active/inactive status."""

    is_active: bool = Field(default=True, description="Active status flag")
