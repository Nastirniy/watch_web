from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import inspect as sa_inspect

from app.backend.models.media import MediaFile
from app.backend.api.v1.models.response.users import UserPublicResponse
from app.backend.utils.validators import PlainTextModel
from app.backend.config import settings
from app.backend.services.media.storyboards import client_status
from app.backend.services.media.urls import thumbnail_urls
from app.backend.api.shared.openapi_docs import header


class ThumbnailUrls(BaseModel):
    """Signed, expiring URLs a browser can load directly (no bearer token)"""
    small: str
    medium: str
    large: str
    preview: str


def media_info_payload(media: MediaFile) -> Dict[str, Any]:
    """Serialisable view of a MediaFile row including derived thumbnail fields"""
    state = sa_inspect(media)
    user = media.user if "user" not in state.unloaded else None
    urls = thumbnail_urls(media)
    return {
        "id": media.id,
        "original_filename": media.original_filename,
        "stored_filename": media.stored_filename,
        "file_size": media.file_size,
        "mime_type": media.mime_type,
        "media_type": media.media_type,
        "width": media.width,
        "height": media.height,
        "uploaded_at": media.uploaded_at,
        "observation_id": media.observation_id,
        "is_deleted": media.is_deleted,
        "thumbnail_status": media.thumbnail_status,
        "thumbnail_urls": urls,
        "storyboard_status": client_status(media, settings.storage.storyboard),
        "poster_generated_at": media.poster_generated_at,
        "duration_seconds": media.duration_seconds,
        "video_codec": media.video_codec,
        "audio_codec": media.audio_codec,
        "playback_type": media.playback_type or (media.mime_type if media.media_type in ("video", "audio") else None),
        "user": user,
    }


class MediaFileInfo(PlainTextModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_filename: str
    stored_filename: str
    file_size: int
    mime_type: str
    media_type: str
    width: Optional[int]
    height: Optional[int]
    uploaded_at: datetime
    observation_id: Optional[int] = None
    # Archived files stay linked to their observation but are hidden from it (POST /media/{id}/restore undoes)
    is_deleted: bool = False

    # ready | pending | processing | failed | unsupported (see ThumbnailStatus)
    thumbnail_status: str = "unsupported"
    # Present only while thumbnail_status is ready
    thumbnail_urls: Optional[ThumbnailUrls] = None
    # Video only: when the poster frame was extracted
    poster_generated_at: Optional[datetime] = None
    # ready | pending | processing | failed | unsupported (see StoryboardStatus); GET /media/{id}/storyboard serves it
    storyboard_status: str = "unsupported"

    # Video and audio: container/codec metadata read from the file headers
    duration_seconds: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    # Hand this to HTMLMediaElement.canPlayType() before building a player;
    # falls back to the bare mime_type until the file has been probed
    playback_type: Optional[str] = None

    # Nested projection named after the ORM relationship (uploader)
    user: Optional[UserPublicResponse] = None

    @model_validator(mode="before")
    @classmethod
    def _from_media_row(cls, data: Any) -> Any:
        if isinstance(data, MediaFile):
            return media_info_payload(data)
        return data


class ShareLinkResponse(BaseModel):
    media_id: int
    size: str
    format: str
    url: str
    expires_at: datetime


class WorkerInfo(BaseModel):
    name: str
    kind: str
    hostname: str
    pid: int
    started_at: datetime
    last_seen_at: datetime
    jobs_done: int
    jobs_failed: int
    stale: bool
    details: Optional[str] = None


class PosterStatusResponse(BaseModel):
    """Queue depth by poster status plus every worker's last heartbeat"""
    posters: Dict[str, int]
    workers: List[WorkerInfo]
    worker_alive: bool


class PosterReconcileResponse(BaseModel):
    queued_untracked: int
    requeued_missing: int
    retried_failed: int


class ThumbnailRegenerateResponse(BaseModel):
    media_id: int
    thumbnail_status: str
    message: str


class UploadResultResponse(BaseModel):
    """Result of every upload route (multipart, nginx offload, resumable complete)"""
    message: str
    media_file_id: int
    original_filename: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    # ready | pending | processing | failed | unsupported; a video starts pending until the worker extracts its poster
    thumbnail_status: Optional[str] = None
    thumbnail_urls: Optional[ThumbnailUrls] = None
    # Resumable uploads only: the session that produced the file
    upload_id: Optional[str] = None


def upload_result(media_file: MediaFile, transport: str, upload_id: Optional[str] = None) -> UploadResultResponse:
    """The body every upload route answers once the file is stored and recorded"""
    return UploadResultResponse(
        message=f"File uploaded successfully ({transport})",
        media_file_id=media_file.id,
        original_filename=media_file.original_filename,
        file_size=media_file.file_size,
        mime_type=media_file.mime_type,
        thumbnail_status=media_file.thumbnail_status,
        thumbnail_urls=thumbnail_urls(media_file),
        upload_id=upload_id,
    )


class StorageStatisticsResponse(BaseModel):
    """Counts and sizes of the stored media (administration)"""
    total_files: int
    total_file_size_bytes: int
    active_count: int
    active_size_bytes: int
    deleted_count: int
    deleted_size_bytes: int
    # Active files by media_type
    media_types: Dict[str, int]
    # Present when the storage directory exists
    filesystem_files: Optional[int] = None
    filesystem_size_bytes: Optional[int] = None


class MediaFormatInfo(BaseModel):
    id: Optional[int] = None
    content_type: str
    extensions: List[str]
    label: str
    aliases: List[str] = []
    max_file_size_bytes: int


class MediaFormatsResponse(BaseModel):
    """Accepted upload formats grouped by media type; the same list is sent as Accept-Post on 415 answers"""
    media_types: List[str]
    formats: Dict[str, List[MediaFormatInfo]]
    # Server ceiling (storage.max_file_size_gb); every per-format limit is at or below it
    max_file_size_bytes: int


# Headers of the file and thumbnail routes
ORIGINAL_FILE_HEADERS = {
    "Content-Disposition": header('attachment (download) or inline (stream), with filename="<original name>"'),
    "ETag": header("sha256 of the file, quoted"),
    "X-File-Hash": header("sha256 of the file"),
    "Accept-Ranges": header("bytes: the file can be read in parts with Range"),
    "Cache-Control": header("private, max-age=3600"),
}
THUMBNAIL_PENDING_HEADERS = {
    "Retry-After": header("Seconds to wait before asking again", "integer"),
    "X-Thumbnail-Status": header("pending or processing"),
    "Cache-Control": header("no-store"),
}
STORYBOARD_PENDING_HEADERS = {
    "Retry-After": header("Seconds to wait before asking again", "integer"),
    "X-Storyboard-Status": header("pending or processing"),
    "Cache-Control": header("no-store"),
}
STORYBOARD_HEADERS = {
    "Cache-Control": header("private, max-age=<lifetime of the signed sheet URLs inside>"),
}
