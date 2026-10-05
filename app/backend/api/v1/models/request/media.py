from pydantic import BaseModel, Field

from app.backend.enums.general import ThumbnailFormat, ThumbnailSize


class ListAttachedMediaRequest(BaseModel):
    observation_id: int


class ShareLinkRequest(BaseModel):
    """Public, expiring thumbnail link. ttl_seconds is capped by storage.thumbnail.share_max_ttl_seconds."""
    size: ThumbnailSize = ThumbnailSize.MEDIUM
    format: ThumbnailFormat = ThumbnailFormat.AUTO
    ttl_seconds: int = Field(default=3600, ge=60)


class PosterReconcileRequest(BaseModel):
    """Repair poster state against the filesystem; retry_failed also re-queues failed videos."""
    retry_failed: bool = False
