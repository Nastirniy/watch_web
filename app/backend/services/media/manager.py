"""Process-wide MediaManager built from settings.

Constructed on first use rather than at import time so that importing the
routes never touches the filesystem, and so tests can point the storage at a
temporary directory by patching settings before the first call.
"""
from functools import lru_cache

from app.backend.config import settings
from app.backend.services.media_manager import MediaManager


@lru_cache(maxsize=1)
def get_media_manager() -> MediaManager:
    return MediaManager(
        storage_path=settings.storage.upload_path,
        temp_path=settings.storage.temp_path,
        enable_deduplication=True,
        enable_nginx=settings.storage.enable_nginx,
    )


def reset_media_manager() -> None:
    """Drop the cached instance (tests)"""
    get_media_manager.cache_clear()
