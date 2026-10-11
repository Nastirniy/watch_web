"""Client-facing thumbnail URLs derived from a MediaFile row and settings.

Three URL shapes share one signed imgproxy path:
- internal (X-Accel-Redirect): {imgproxy.nginx_internal_prefix}{signed path}, no expiry
- renderer (dev proxy and pre-warm): {imgproxy.url}{signed path}
- public (list responses, share links): {public_base_url}{public_prefix}{signed path with exp:}
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from app.backend.config import settings
from app.backend.enums.general import ThumbnailFormat, ThumbnailSize, ThumbnailStatus
from app.backend.models.media import MediaFile
from app.backend.models.storyboard import storyboard_relative_dir
from app.backend.services.media.imgproxy import ImgproxySigner, build_processing_path, signed_path


def get_signer() -> ImgproxySigner:
    cfg = settings.storage.imgproxy
    return ImgproxySigner.from_hex(cfg.key, cfg.salt)


def cache_buster_for(generated_at: Optional[datetime]) -> Optional[str]:
    """imgproxy cache buster from a row's generation time: whole seconds, naive means UTC (MySQL DATETIME keeps no
    fraction). None while the time is unknown."""
    if generated_at is None:
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return str(int(generated_at.timestamp()))


def processing_path_for(
    media: MediaFile,
    size: str = ThumbnailSize.MEDIUM.value,
    fmt: str = ThumbnailFormat.AUTO.value,
    expires_at: Optional[datetime] = None,
) -> Optional[str]:
    """Signed imgproxy path for the media's thumbnail source, or None when nothing can be rendered yet.

    A poster carries cb: from poster_generated_at: a regenerated poster is a new file under the same path, and
    without it nginx would keep serving the render of the old one until its max-age. Image paths have no cb:
    (the original is never replaced under its path).
    """
    source = media.thumbnail_source_path
    if not source:
        return None
    width, height = settings.storage.thumbnail.size_for(size)
    path = build_processing_path(
        source, width, height, fmt=fmt,
        quality=settings.storage.thumbnail.quality,
        expires_at=expires_at,
        cache_buster=cache_buster_for(media.poster_generated_at) if media.is_video else None,
    )
    return signed_path(get_signer(), path)


def internal_url(media: MediaFile, size: str, fmt: str) -> Optional[str]:
    """Path handed to nginx through X-Accel-Redirect"""
    path = processing_path_for(media, size, fmt)
    if path is None:
        return None
    return f"{settings.storage.imgproxy.nginx_internal_prefix.rstrip('/')}{path}"


def renderer_url(media: MediaFile, size: str, fmt: str) -> Optional[str]:
    """Direct imgproxy URL, used when nginx is disabled and for pre-warming"""
    path = processing_path_for(media, size, fmt)
    if path is None:
        return None
    return f"{settings.storage.imgproxy.url.rstrip('/')}{path}"


def quantised_expiry(ttl_seconds: int, now: Optional[datetime] = None) -> datetime:
    """Expiry aligned to a ttl-sized window so every URL issued in the window is byte-identical.

    nginx caches by URI, and the imgproxy signature covers the exp: option, so
    an expiry taken from the clock would give every request a new cache key.
    The result is always at least ttl and at most 2 * ttl in the future.
    """
    ttl = max(60, int(ttl_seconds))
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    return datetime.fromtimestamp((now_ts // ttl + 2) * ttl, tz=timezone.utc)


def share_expiry(ttl_seconds: int, now: Optional[datetime] = None) -> datetime:
    """Expiry of a share link: stable inside a window and never later than now + ttl.

    Window w = max(1, ttl // 10), expires_at = floor(now / w) * w + ttl, which lies in (now + ttl - w, now + ttl].
    Links issued in the same window are byte-identical (nginx cache key); the link lives more than ttl - w, which is
    at least 0.9 * ttl from ttl 10 on (more than 54 s for the smallest share ttl, 60 s).
    """
    ttl = int(ttl_seconds)
    window = max(1, ttl // 10)
    now_ts = int((now or datetime.now(timezone.utc)).timestamp())
    return datetime.fromtimestamp(now_ts // window * window + ttl, tz=timezone.utc)


def public_url(
    media: MediaFile, size: str, fmt: str, ttl_seconds: int, base_url: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> Optional[tuple[str, datetime]]:
    """Expiring URL a browser can load without a bearer token. Returns (url, expires_at).

    The expiry is quantised_expiry(ttl) (between ttl and 2 * ttl ahead) unless the caller passes one."""
    if expires_at is None:
        expires_at = quantised_expiry(ttl_seconds)
    path = processing_path_for(media, size, fmt, expires_at=expires_at)
    if path is None:
        return None
    cfg = settings.storage.imgproxy
    origin = cfg.public_base_url if base_url is None else base_url
    url = f"{origin.rstrip('/')}{cfg.public_prefix.rstrip('/')}{path}"
    return url, expires_at


def prewarm_url(media: MediaFile, size: str, fmt: str) -> Optional[str]:
    """Same public URL the next list response will hand out, addressed to nginx so the cache gets filled"""
    cfg = settings.storage.imgproxy
    origin = cfg.prewarm_base_url or cfg.public_base_url
    if not origin:
        return None
    built = public_url(media, size, fmt, settings.storage.thumbnail.url_ttl_seconds, base_url=origin)
    return built[0] if built else None


def storyboard_sheet_url(
    media_id: int,
    sheet_index: int,
    expires_at: datetime,
    base_url: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    """Expiring public URL of one storyboard sheet, served unchanged (source size, JPEG) so #xywh stays valid.

    base_url replaces public_base_url as the origin (the route passes the request origin when that is empty:
    cue URLs must be absolute, a player resolves them against a blob: URL). generated_at (the row's) becomes
    imgproxy's cache buster: a regenerated storyboard, maybe with another layout, gets new URLs, never the sheets
    nginx cached from the previous one. Whole seconds, naive means UTC (MySQL DATETIME keeps no fraction).
    """
    cfg = settings.storage.imgproxy
    source = f"{storyboard_relative_dir(media_id)}/sheet-{sheet_index:03d}.jpg"
    path = build_processing_path(
        source, 0, 0, fmt=ThumbnailFormat.JPEG.value, expires_at=expires_at,
        cache_buster=cache_buster_for(generated_at),
    )
    origin = cfg.public_base_url if base_url is None else base_url
    return f"{origin.rstrip('/')}{cfg.public_prefix.rstrip('/')}{signed_path(get_signer(), path)}"


def thumbnail_urls(media: MediaFile) -> Optional[Dict[str, str]]:
    """Signed public URLs for every size, valid for thumbnail.url_ttl_seconds; None until a thumbnail can be rendered"""
    if media.thumbnail_status != ThumbnailStatus.READY.value:
        return None
    ttl = settings.storage.thumbnail.url_ttl_seconds
    result: Dict[str, str] = {}
    for size in ThumbnailSize:
        built = public_url(media, size.value, ThumbnailFormat.AUTO.value, ttl)
        if built is None:
            return None
        result[size.value] = built[0]
    return result
