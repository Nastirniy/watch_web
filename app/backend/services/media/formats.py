"""Accepted media formats: the registry read from media_formats and the checks built on it.

Every upload is checked against the active rows four times: the declared
Content-Type, the file extension, the MIME type detected from the bytes, and
the size against the format's max_file_size_bytes. Anything outside is
answered with 415 (format) or 413 (size), with an Accept-Post header naming
what is accepted. GET /media/formats returns the same list for clients;
administrators manage the rows through /media-formats.

DEFAULT_FORMATS seeds a fresh database and is the reference for what
imgproxy renders, ffmpeg decodes and browsers play.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import MediaType
from app.backend.enums.errors import ErrorCode
from app.backend.models.media_format import MediaFormat
from app.backend.services.api_errors import CodedError

# Declared Content-Type values that mean "detect from the bytes"; never stored
OPAQUE_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream"}

MB = 1024 * 1024
GB = 1024 * MB

# Registry cache: uploads are rare compared with reads, but a query per upload
# is still avoidable; writes call invalidate_registry_cache() so the process
# that changed a row sees it at once and the others within the TTL.
REGISTRY_CACHE_TTL_SECONDS = 15.0


@dataclass(frozen=True)
class FormatSpec:
    media_type: str
    content_type: str                  # canonical Content-Type, lower case
    extensions: Tuple[str, ...]        # with the dot, lower case
    label: str
    max_file_size_bytes: int
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    id: Optional[int] = None
    is_active: bool = True
    sort_order: int = 0

    @property
    def all_mimes(self) -> Tuple[str, ...]:
        return (self.content_type, *self.aliases)

    @classmethod
    def from_row(cls, row: MediaFormat) -> "FormatSpec":
        return cls(
            media_type=row.media_type, content_type=row.content_type,
            extensions=tuple(row.extensions or ()), label=row.label,
            max_file_size_bytes=int(row.max_file_size_bytes), aliases=tuple(row.aliases or ()),
            id=row.id, is_active=row.is_active, sort_order=row.sort_order,
        )


def hard_limit_bytes() -> int:
    """Absolute ceiling for any format: storage.max_file_size_gb (nginx client_max_body_size must match)"""
    return int(settings.storage.max_file_size_gb) * GB


def _default_size(media_type: str) -> int:
    return {
        MediaType.IMAGE.value: 50 * MB,
        MediaType.AUDIO.value: 1 * GB,
        MediaType.VIDEO.value: hard_limit_bytes(),
    }[media_type]


def default_formats() -> Tuple[FormatSpec, ...]:
    """Seed data: keep it short, every entry must be renderable by imgproxy, decodable by ffmpeg or playable in a browser"""
    image, video, audio = MediaType.IMAGE.value, MediaType.VIDEO.value, MediaType.AUDIO.value
    rows = (
        (image, "image/jpeg", (".jpg", ".jpeg"), "JPEG", ("image/pjpeg",)),
        (image, "image/png", (".png",), "PNG", ()),
        (image, "image/webp", (".webp",), "WebP", ()),
        (image, "image/gif", (".gif",), "GIF", ()),
        (video, "video/mp4", (".mp4", ".m4v"), "MP4 (H.264/H.265)", ("video/x-m4v",)),
        (video, "video/webm", (".webm",), "WebM", ()),
        (video, "video/quicktime", (".mov",), "QuickTime MOV", ()),
        (video, "video/x-matroska", (".mkv",), "Matroska MKV", ("video/matroska",)),
        (audio, "audio/mpeg", (".mp3",), "MP3", ("audio/mp3",)),
        (audio, "audio/wav", (".wav",), "WAV", ("audio/x-wav", "audio/wave", "audio/vnd.wave")),
        (audio, "audio/flac", (".flac",), "FLAC", ("audio/x-flac",)),
        (audio, "audio/ogg", (".ogg", ".oga", ".opus"), "Ogg Vorbis / Opus", ("audio/opus", "application/ogg")),
        (audio, "audio/mp4", (".m4a",), "AAC in MP4 (M4A)", ("audio/x-m4a", "audio/aac")),
    )
    return tuple(
        FormatSpec(media_type=mt, content_type=ct, extensions=exts, label=label,
                   max_file_size_bytes=_default_size(mt), aliases=aliases, sort_order=index)
        for index, (mt, ct, exts, label, aliases) in enumerate(rows)
    )


class UnsupportedMediaFormat(CodedError):
    """Extension or detected MIME type outside the accepted formats; the route answers 415"""

    def __init__(self, code: ErrorCode, media_type: Optional[str] = None, **params):
        super().__init__(code, media_type=media_type, **params)
        self.media_type = media_type


class FileTooLarge(ValueError):
    """File exceeds the format's max_file_size_bytes; the route answers 413 with the limits of its format"""

    def __init__(self, message: str, limit_bytes: int, actual_bytes: Optional[int] = None):
        super().__init__(message)
        self.limit_bytes = limit_bytes
        self.actual_bytes = actual_bytes


def normalise_content_type(value: Optional[str]) -> Optional[str]:
    """'image/JPEG; charset=binary' -> 'image/jpeg'; None or empty stays None"""
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def extension_of(filename: Optional[str]) -> Optional[str]:
    if not filename or "." not in filename:
        return None
    ext = "." + filename.rsplit(".", 1)[1].lower()
    return ext if len(ext) > 1 else None


class FormatRegistry:
    """The accepted formats at one point in time (active, non-deleted rows)"""

    def __init__(self, specs: List[FormatSpec]):
        self.specs = [spec for spec in specs if spec.is_active]

    # ---- queries -------------------------------------------------------

    def media_types(self) -> List[str]:
        seen: List[str] = []
        for spec in self.specs:
            if spec.media_type not in seen:
                seen.append(spec.media_type)
        return seen

    def formats_for(self, media_type: Optional[str] = None) -> List[FormatSpec]:
        if media_type is None:
            return list(self.specs)
        wanted = media_type.lower()
        return [spec for spec in self.specs if spec.media_type == wanted]

    def content_types(self, media_type: Optional[str] = None) -> List[str]:
        """Canonical Content-Type values, in registry order"""
        return [spec.content_type for spec in self.formats_for(media_type)]

    def mime_set(self, media_type: Optional[str] = None) -> set:
        """Canonical values plus aliases, for matching what detectors and clients send"""
        result = set()
        for spec in self.formats_for(media_type):
            result.update(spec.all_mimes)
        return result

    def extensions(self, media_type: Optional[str] = None) -> List[str]:
        result: List[str] = []
        for spec in self.formats_for(media_type):
            result.extend(ext for ext in spec.extensions if ext not in result)
        return result

    def accept_post_header(self, media_type: Optional[str] = None) -> str:
        return ", ".join(self.content_types(media_type))

    def resolve(self, media_type: str, filename: Optional[str]) -> Optional[FormatSpec]:
        """The format an upload falls under, by media_type and file extension"""
        ext = extension_of(filename)
        if ext is None:
            return None
        for spec in self.formats_for(media_type):
            if ext in spec.extensions:
                return spec
        return None

    def is_accepted_content_type(self, media_type: str, content_type: Optional[str]) -> bool:
        """True when the declared Content-Type is one of the accepted formats or opaque (detect from bytes)"""
        ct = normalise_content_type(content_type)
        if ct is None or ct in OPAQUE_CONTENT_TYPES:
            return True
        return ct in self.mime_set(media_type)

    def media_type_for_mime(self, mime: Optional[str]) -> Optional[str]:
        ct = normalise_content_type(mime)
        for spec in self.specs:
            if ct in spec.all_mimes:
                return spec.media_type
        return None

    def payload(self) -> Dict[str, List[Dict[str, object]]]:
        """Serialisable registry grouped by media type, for GET /media/formats"""
        grouped: Dict[str, List[Dict[str, object]]] = {}
        for spec in self.specs:
            grouped.setdefault(spec.media_type, []).append({
                "id": spec.id,
                "content_type": spec.content_type,
                "extensions": list(spec.extensions),
                "label": spec.label,
                "aliases": list(spec.aliases),
                "max_file_size_bytes": spec.max_file_size_bytes,
            })
        return grouped


# ---- loading ---------------------------------------------------------------

_cache: Tuple[float, Optional[FormatRegistry]] = (0.0, None)


def invalidate_registry_cache() -> None:
    global _cache
    _cache = (0.0, None)


async def load_registry(session: AsyncSession, use_cache: bool = True) -> FormatRegistry:
    """Active formats from the database (cached for REGISTRY_CACHE_TTL_SECONDS)"""
    global _cache
    expires, cached = _cache
    if use_cache and cached is not None and time.monotonic() < expires:
        return cached
    rows = (await session.exec(
        select(MediaFormat)
        .where(MediaFormat.is_deleted == False, MediaFormat.is_active == True)
        .order_by(MediaFormat.sort_order, MediaFormat.id)
    )).all()
    registry = FormatRegistry([FormatSpec.from_row(row) for row in rows])
    _cache = (time.monotonic() + REGISTRY_CACHE_TTL_SECONDS, registry)
    return registry


async def seed_media_formats(session: AsyncSession) -> int:
    """Insert every default format whose content_type is missing (never touches existing rows). Returns the count added."""
    existing = set((await session.exec(select(MediaFormat.content_type))).all())
    added = 0
    for spec in default_formats():
        if spec.content_type in existing:
            continue
        session.add(MediaFormat(
            media_type=spec.media_type, content_type=spec.content_type, label=spec.label,
            extensions=list(spec.extensions), aliases=list(spec.aliases),
            max_file_size_bytes=min(spec.max_file_size_bytes, hard_limit_bytes()),
            is_active=True, sort_order=spec.sort_order,
        ))
        added += 1
    if added:
        await session.commit()
        invalidate_registry_cache()
    return added
