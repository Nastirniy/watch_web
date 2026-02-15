from typing import Optional, TYPE_CHECKING
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
from app.backend.utils.datetime_utils import get_default_timezone_datetime

if TYPE_CHECKING:
    from app.backend.models.observation import Observation
    from app.backend.models.user import User


class MediaFile(SQLModel, table=True):
    __tablename__ = "media_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    observation_id: Optional[int] = Field(default=None, foreign_key="observations.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)

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

    thumbnail_path: Optional[str] = Field(default=None, max_length=500)
    thumbnail_error: Optional[str] = Field(default=None, max_length=500)

    uploaded_at: datetime = Field(default_factory=get_default_timezone_datetime, index=True)
    is_deleted: bool = Field(default=False, index=True)

    # Relationships
    observation: Optional["Observation"] = Relationship(back_populates="media_files")
    user: Optional["User"] = Relationship()

    @property
    def thumbnail_id(self) -> Optional[int]:
        """Convenience property to get thumbnail ID (currently same as media_id if exists)"""
        return self.id if self.thumbnail_path else None
