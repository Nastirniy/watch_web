from typing import Any, Dict, List, Optional

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import inspect as sa_inspect

from app.backend.enums.general import TIMED_MEDIA_TYPES, UploadTransport
from app.backend.models.media import MediaFile
from app.backend.api.v1.models.response.users import UserPublicResponse
from app.backend.utils.validators import PlainTextModel, ResponseDatetime, ResponseModel
from app.backend.config import settings
from app.backend.services.media.storyboards import client_status
from app.backend.services.media.urls import thumbnail_urls
from app.backend.api.shared.openapi_docs import header
from app.backend.enums.media_types import Disposition


class ThumbnailUrls(ResponseModel):
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
        "playback_type": media.playback_type or (media.mime_type if media.media_type in TIMED_MEDIA_TYPES else None),
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
    uploaded_at: ResponseDatetime
    observation_id: Optional[int] = None
    # Archived files stay linked to their observation but are hidden from it (POST /media/{id}/restore undoes)
    is_deleted: bool = False

    # ready | pending | processing | failed | unsupported (see ThumbnailStatus)
    thumbnail_status: str = "unsupported"
    # Present only while thumbnail_status is ready
    thumbnail_urls: Optional[ThumbnailUrls] = None
    # Video only: when the poster frame was extracted
    poster_generated_at: Optional[ResponseDatetime] = None
    # ready | pending | processing | failed | unsupported (see StoryboardStatus); GET /media/{id}/storyboard serves it
    storyboard_status: str = "unsupported"

    # Video and audio: container/codec metadata read from the file headers
    duration_seconds: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    playback_type: Optional[str] = Field(
        default=None,
        description="The format with its codecs (RFC 6381), for HTMLMediaElement.canPlayType(); the bare mime_type until "
                    "the file has been probed. The API states the format and takes no view on what a browser plays: "
                    "whether to show a player is the web's decision (Chromium answers \"\" for Matroska and plays it, "
                    "Firefox plays no Matroska)")

    # Nested projection named after the ORM relationship (uploader)
    user: Optional[UserPublicResponse] = None

    @model_validator(mode="before")
    @classmethod
    def _from_media_row(cls, data: Any) -> Any:
        if isinstance(data, MediaFile):
            return media_info_payload(data)
        return data


class ShareLinkResponse(ResponseModel):
    media_id: int
    size: str
    format: str
    url: str
    expires_at: ResponseDatetime


class WorkerInfo(ResponseModel):
    name: str
    kind: str
    hostname: str
    pid: int
    started_at: ResponseDatetime
    last_seen_at: ResponseDatetime
    jobs_done: int
    jobs_failed: int
    stale: bool
    details: Optional[str] = None


class PosterStatusResponse(ResponseModel):
    """Queue depth by poster status plus every worker's last heartbeat"""
    posters: Dict[str, int]
    workers: List[WorkerInfo]
    worker_alive: bool


class PosterReconcileResponse(ResponseModel):
    # Expired claims on their last attempt (the worker stopped during processing), now failed
    failed_abandoned: int
    queued_untracked: int
    requeued_missing: int
    retried_failed: int


class ThumbnailRegenerateResponse(ResponseModel):
    media_id: int
    thumbnail_status: str
    message: str


class UploadResultResponse(ResponseModel):
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


def upload_result(media_file: MediaFile, transport: UploadTransport, upload_id: Optional[str] = None,
                  repeated: bool = False) -> UploadResultResponse:
    """The body every upload route answers once the file is stored and recorded (repeated: a complete of an upload
    session that completed before)"""
    return UploadResultResponse(
        message=f"File uploaded successfully ({transport.value}{', already completed' if repeated else ''})",
        media_file_id=media_file.id,
        original_filename=media_file.original_filename,
        file_size=media_file.file_size,
        mime_type=media_file.mime_type,
        thumbnail_status=media_file.thumbnail_status,
        thumbnail_urls=thumbnail_urls(media_file),
        upload_id=upload_id,
    )


class StorageStatisticsResponse(ResponseModel):
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


class MediaFormatInfo(ResponseModel):
    id: Optional[int] = None
    content_type: str
    extensions: List[str]
    label: str
    aliases: List[str] = []
    max_file_size_bytes: int


class MediaFormatsResponse(ResponseModel):
    """Accepted upload formats grouped by media type; the same list is sent as Accept-Post on 415 answers"""
    media_types: List[str]
    formats: Dict[str, List[MediaFormatInfo]]
    # Server ceiling (storage.max_file_size_gb); every per-format limit is at or below it
    max_file_size_bytes: int


# Headers of every file answer (services/files/serve.py file_headers); behind nginx nginx-app.conf sends them
FILE_SAFETY_HEADERS = {
    "X-Content-Type-Options": header("nosniff"),
    "Content-Security-Policy": header("sandbox; default-src 'none' (the file opened as a page runs sandboxed)"),
}
# What identifies the file on a 200 and a 206 (services/files/serve.py file_headers; behind nginx nginx-app.conf
# passes on the API's)
FILE_IDENTITY_HEADERS = {
    "ETag": header("sha256 of the file, quoted (\"<64 hex digits>\"): send it back in If-None-Match or If-Range"),
    "Repr-Digest": header("SHA-256 of the whole file, on a 206 as well (RFC 9530): "
                          "sha-256=:<base64 of the 32 digest bytes>:"),
}
PARTIAL_FILE_HEADERS = {"Content-Range": header("bytes start-end/size"), **FILE_IDENTITY_HEADERS}
# The 304 of a stored file (RFC 9110 section 15.4.5: what a 200 would send of these), with empty_response
FILE_NOT_MODIFIED = ("If-None-Match matched (one of its tags equals the ETag of the file by weak comparison, or it is "
                     "*): no body, the copy the client holds is current. Comes after every access check")
FILE_NOT_MODIFIED_HEADERS = {
    "ETag": FILE_IDENTITY_HEADERS["ETag"],
    "Cache-Control": header("private, max-age=3600"),
}


def disposition_header(kind: str, name: str) -> dict:
    # kind: a Disposition value, or prose naming the ones a route answers
    """Content-Disposition with the name twice (RFC 6266, RFC 8187)"""
    return header(f"{kind}; filename=\"<{name}, ASCII fallback>\"; filename*=UTF-8''<{name}, UTF-8 percent-encoded>")


# Headers of the file and thumbnail routes
ORIGINAL_FILE_HEADERS = {
    "Content-Disposition": disposition_header(f"{Disposition.ATTACHMENT} (download) or {Disposition.INLINE} (stream)", "original name"),
    **FILE_SAFETY_HEADERS,
    **FILE_IDENTITY_HEADERS,
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
