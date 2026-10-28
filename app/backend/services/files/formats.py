"""Accepted file formats (media and documents): the registry read from media_formats and the checks built on it.

Every upload is checked against the active rows four times: the declared
Content-Type, the file extension, the MIME type detected from the bytes
(services/files/sniff.py, the one sniffer), and the size against the format's
max_file_size_bytes. Anything outside is
answered with 415 (format) or 413 (size), with an Accept-Post header naming
what is accepted. GET /media/formats returns the same list for clients;
administrators manage the rows through /media-formats.

DEFAULT_FORMATS seeds a fresh database and is the reference for what
imgproxy renders, ffmpeg decodes and browsers play.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple, Union

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import MediaType, UploadPurpose
from app.backend.services.files.names import extension_of
from app.backend.enums.errors import ErrorCode
from app.backend.models.media_format import MediaFormat
from app.backend.errors.coded import CodedError
from app.backend.utils.logging_config import get_api_logger

_logger = get_api_logger()

# Declared Content-Type values that mean "detect from the bytes"; never stored
OPAQUE_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream"}

MB = 1024 * 1024
GB = 1024 * MB

# Which media types of the registry an upload purpose may use
PURPOSE_MEDIA_TYPES: Dict[UploadPurpose, FrozenSet[str]] = {
    UploadPurpose.OBSERVATION_MEDIA: frozenset({MediaType.IMAGE.value, MediaType.VIDEO.value, MediaType.AUDIO.value}),
    UploadPurpose.USER_GUIDE: frozenset({MediaType.DOCUMENT.value}),
}

# The top-level Content-Type types a format of each media type may have (the one family rule)
ALLOWED_TOP_LEVEL: Dict[str, FrozenSet[str]] = {
    MediaType.IMAGE.value: frozenset({"image"}),
    MediaType.VIDEO.value: frozenset({"video"}),
    MediaType.AUDIO.value: frozenset({"audio"}),
    MediaType.DOCUMENT.value: frozenset({"application", "text"}),
}

# Types that run code in a browser or on a desktop when opened: never a format's content type or alias
FORBIDDEN_CONTENT_TYPES: FrozenSet[str] = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/javascript", "text/javascript",
    "application/x-msdownload", "application/x-sh", "application/java-archive",
})


def content_type_allowed(media_type: str, content_type: str) -> bool:
    """True when the Content-Type belongs to the family of the media type (image/* for image, application/* or text/*
    for document, ...)"""
    top = (content_type or "").split("/", 1)[0].strip().lower()
    return top in ALLOWED_TOP_LEVEL.get((media_type or "").strip().lower(), frozenset())

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
    def effective_limit_bytes(self) -> int:
        """The limit uploads are held to: the format's own, never above the server ceiling (it can be lowered after rows are written)"""
        return min(self.max_file_size_bytes, hard_limit_bytes())

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
    """Absolute ceiling for any format: storage.max_file_size_gb (nginx client_max_body_size stays above it)"""
    return int(settings.storage.max_file_size_gb) * GB


def _default_size(media_type: str) -> int:
    return {
        MediaType.IMAGE.value: 50 * MB,
        MediaType.AUDIO.value: 1 * GB,
        MediaType.VIDEO.value: hard_limit_bytes(),
        MediaType.DOCUMENT.value: 100 * MB,
    }[media_type]


def default_formats() -> Tuple[FormatSpec, ...]:
    """Seed data: keep it short, every entry must be renderable by imgproxy, decodable by ffmpeg or playable in a browser"""
    image, video, audio = MediaType.IMAGE.value, MediaType.VIDEO.value, MediaType.AUDIO.value
    document = MediaType.DOCUMENT.value
    wordml = "application/vnd.openxmlformats-officedocument."
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
        # Document aliases are what Debian's libmagic (the api image) reports for real files: OOXML, ODF and Word 97
        # by their own types, Markdown as text/plain, a CSV of fewer than three lines as text/plain, RTF as text/rtf
        (document, "application/pdf", (".pdf",), "PDF", ()),
        (document, wordml + "wordprocessingml.document", (".docx",), "Word DOCX", ()),
        (document, wordml + "spreadsheetml.sheet", (".xlsx",), "Excel XLSX", ()),
        (document, wordml + "presentationml.presentation", (".pptx",), "PowerPoint PPTX", ()),
        (document, "application/vnd.oasis.opendocument.text", (".odt",), "OpenDocument text", ()),
        (document, "application/msword", (".doc",), "Word 97-2003", ()),
        (document, "text/markdown", (".md", ".markdown"), "Markdown", ("text/plain",)),
        (document, "text/plain", (".txt",), "Plain text", ()),
        (document, "text/csv", (".csv",), "CSV", ("text/plain",)),
        (document, "application/rtf", (".rtf",), "RTF", ("text/rtf",)),
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

    def restricted(self, purpose: UploadPurpose) -> "FormatRegistry":
        """The same registry limited to the media types of an upload purpose"""
        allowed = PURPOSE_MEDIA_TYPES[purpose]
        return FormatRegistry([spec for spec in self.specs if spec.media_type in allowed])

    def accept_post_header(self, scope: Union[UploadPurpose, str, None] = None) -> str:
        """Accept-Post value: the Content-Types of a purpose, of one media type, or of every format"""
        if isinstance(scope, UploadPurpose):
            return self.restricted(scope).accept_post_header()
        return ", ".join(self.content_types(scope))

    def resolve(self, purpose: UploadPurpose, filename: Optional[str]) -> Optional[FormatSpec]:
        """The format an upload falls under, by purpose and file extension"""
        ext = extension_of(filename)
        if not ext:
            return None
        allowed = PURPOSE_MEDIA_TYPES[purpose]
        for spec in self.specs:
            if spec.media_type in allowed and ext in spec.extensions:
                return spec
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
                "max_file_size_bytes": spec.effective_limit_bytes,
            })
        return grouped


# ---- loading ---------------------------------------------------------------

_cache: Tuple[float, Optional[FormatRegistry]] = (0.0, None)
# Bumped by every invalidate: a read that began before one must not be stored (it may predate the write)
_generation = 0


def invalidate_registry_cache() -> None:
    global _cache, _generation
    _generation += 1
    _cache = (0.0, None)


async def load_registry(session: AsyncSession, use_cache: bool = True) -> FormatRegistry:
    """Active formats from the database (cached for REGISTRY_CACHE_TTL_SECONDS)"""
    global _cache
    expires, cached = _cache
    if use_cache and cached is not None and time.monotonic() < expires:
        return cached
    generation = _generation
    rows = (await session.exec(
        select(MediaFormat)
        .where(MediaFormat.is_deleted == False, MediaFormat.is_active == True)
        .order_by(MediaFormat.sort_order, MediaFormat.id)
    )).all()
    registry = FormatRegistry([FormatSpec.from_row(row) for row in rows])
    if generation == _generation:
        _cache = (time.monotonic() + REGISTRY_CACHE_TTL_SECONDS, registry)
    return registry


async def observation_registry(session: AsyncSession) -> FormatRegistry:
    """The registry an observation upload works with: image, video and audio formats only"""
    return (await load_registry(session)).restricted(UploadPurpose.OBSERVATION_MEDIA)


def find_conflict(rows: Iterable[MediaFormat], extensions: Iterable[str], aliases: Iterable[str],
                  content_type: str, exclude_id: Optional[int] = None) -> Optional[Tuple[ErrorCode, Dict[str, object]]]:
    """The first non-deleted row sharing an extension or the content_type with the candidate, as (code, params), else None.
    Aliases only list what a detector may report for the format's files: formats may share them, and an alias may
    equal another row's content_type. Unique are the content_type and each extension (`resolve` picks by extension)."""
    extensions = set(extensions)
    for row in rows:
        if row.is_deleted or row.id == exclude_id:
            continue
        clash_ext = sorted(extensions & set(row.extensions or []))
        if clash_ext:
            return ErrorCode.MEDIA_FORMAT_EXTENSION_CLASH, dict(
                extensions=clash_ext, format_content_type=row.content_type, format_id=row.id)
        if content_type == row.content_type:
            return ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_CLASH, dict(
                content_types=[content_type], format_content_type=row.content_type, format_id=row.id)
    return None


async def seed_media_formats(session: AsyncSession) -> int:
    """Insert every default format whose content_type is missing and whose extensions no live row owns
    (an admin may have renamed a default). Never touches existing rows. Returns the count added."""
    rows = list((await session.exec(select(MediaFormat))).all())
    existing = {row.content_type for row in rows}
    added = 0
    for spec in default_formats():
        if spec.content_type in existing:
            continue
        clash = find_conflict(rows, spec.extensions, spec.aliases, spec.content_type)
        if clash is not None:
            _logger.warning(f"Default media format {spec.content_type} not seeded: {clash[0].value} with "
                            f"{clash[1]['format_content_type']}")
            continue
        row = MediaFormat(
            media_type=spec.media_type, content_type=spec.content_type, label=spec.label,
            extensions=list(spec.extensions), aliases=list(spec.aliases),
            max_file_size_bytes=min(spec.max_file_size_bytes, hard_limit_bytes()),
            is_active=True, sort_order=spec.sort_order,
        )
        session.add(row)
        rows.append(row)
        existing.add(spec.content_type)
        added += 1
    if added:
        await session.commit()
        invalidate_registry_cache()
    return added
