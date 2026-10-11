from typing import Optional, TYPE_CHECKING
from datetime import datetime
from pathlib import Path
from sqlalchemy import BigInteger, Column
from sqlmodel import Field, SQLModel, Relationship
from app.backend.config import settings
from app.backend.utils.datetime_utils import get_default_timezone_datetime
from app.backend.enums.general import MediaType, PosterStatus, ThumbnailStatus

if TYPE_CHECKING:
    from app.backend.models.observation import Observation
    from app.backend.models.user import User


POSTER_DIR = "_derived/posters"


class MediaFile(SQLModel, table=True):
    __tablename__ = "media_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    observation_id: Optional[int] = Field(default=None, foreign_key="observations.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)

    original_filename: str = Field(max_length=255)
    stored_filename: str = Field(unique=True, max_length=255)
    file_path: str = Field(max_length=500)
    file_size: int = Field(sa_column=Column(BigInteger, nullable=False))
    mime_type: str = Field(max_length=100)
    file_extension: str = Field(max_length=10)

    media_type: str = Field(max_length=10, index=True)

    file_hash: str = Field(max_length=64, index=True)

    width: Optional[int] = Field(default=None)
    height: Optional[int] = Field(default=None)

    # Video and audio: from ffprobe at upload (API) or during poster extraction (worker)
    duration_seconds: Optional[float] = Field(default=None)
    video_codec: Optional[str] = Field(default=None, max_length=64)
    audio_codec: Optional[str] = Field(default=None, max_length=64)
    # RFC 6381 string for HTMLMediaElement.canPlayType(), e.g. video/mp4; codecs="avc1.64001F, mp4a.40.2"
    playback_type: Optional[str] = Field(default=None, max_length=255)

    # Video poster job state (null for every other media type). Thumbnails of
    # images are rendered on demand and have no state at all.
    poster_status: Optional[str] = Field(default=None, max_length=12, index=True)
    poster_attempts: int = Field(default=0)
    poster_locked_until: Optional[datetime] = Field(default=None)
    poster_worker: Optional[str] = Field(default=None, max_length=64)
    poster_generated_at: Optional[datetime] = Field(default=None)
    poster_error: Optional[str] = Field(default=None, max_length=500)

    uploaded_at: datetime = Field(default_factory=get_default_timezone_datetime, index=True)
    is_deleted: bool = Field(default=False, index=True)

    # Relationships
    observation: Optional["Observation"] = Relationship(back_populates="media_files")
    user: Optional["User"] = Relationship()

    def resolve_path(self, storage_root: Path) -> Path:
        p = Path(self.file_path)
        return p if p.is_absolute() else storage_root / p

    # ---- derived files -------------------------------------------------

    @property
    def is_image(self) -> bool:
        return self.media_type.lower() == MediaType.IMAGE.value

    @property
    def is_video(self) -> bool:
        return self.media_type.lower() == MediaType.VIDEO.value

    @property
    def poster_relative_path(self) -> Optional[str]:
        """Deterministic poster location relative to the upload root: _derived/posters/000/91.jpg"""
        if self.id is None or not self.is_video:
            return None
        return f"{POSTER_DIR}/{self.id // 1000:03d}/{self.id}.jpg"

    def resolve_poster_path(self, storage_root: Path) -> Optional[Path]:
        rel = self.poster_relative_path
        return storage_root / rel if rel else None

    @property
    def megapixels(self) -> Optional[float]:
        """width x height in millions, one decimal; None while the size is unknown"""
        if not (self.width and self.height):
            return None
        return round(self.width * self.height / 1_000_000, 1)

    @property
    def too_large_to_render(self) -> bool:
        """An image above imgproxy's resolution limit (storage.imgproxy.max_src_resolution_mp): every render fails.
        An image of unknown size is rendered as always."""
        if not (self.is_image and self.width and self.height):
            return False
        return self.width * self.height > settings.storage.imgproxy.max_src_resolution_mp * 1_000_000

    @property
    def thumbnail_source_path(self) -> Optional[str]:
        """Path (relative to the upload root) imgproxy renders thumbnails from, or None when nothing can be rendered"""
        if self.is_image:
            return None if self.too_large_to_render else self.file_path
        if self.is_video and self.poster_status == PosterStatus.READY.value:
            return self.poster_relative_path
        return None

    @property
    def thumbnail_status(self) -> str:
        """Client-facing readiness, derived: images are ready unless above the render limit, videos follow the
        poster job (unsupported: the file has no video stream)"""
        if self.is_image:
            return ThumbnailStatus.UNSUPPORTED.value if self.too_large_to_render else ThumbnailStatus.READY.value
        if self.is_video:
            mapping = {
                PosterStatus.READY.value: ThumbnailStatus.READY.value,
                PosterStatus.PROCESSING.value: ThumbnailStatus.PROCESSING.value,
                PosterStatus.FAILED.value: ThumbnailStatus.FAILED.value,
                PosterStatus.UNSUPPORTED.value: ThumbnailStatus.UNSUPPORTED.value,
            }
            return mapping.get(self.poster_status or "", ThumbnailStatus.PENDING.value)
        return ThumbnailStatus.UNSUPPORTED.value
