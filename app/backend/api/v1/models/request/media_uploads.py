from typing import Optional

from pydantic import BaseModel, Field


class UploadSessionCreate(BaseModel):
    """Open a resumable upload. Everything is validated here so a client never sends bytes that will be refused."""
    observation_id: int = Field(..., gt=0)
    media_type: str = Field(..., description="image, video or audio")
    original_filename: str = Field(..., min_length=1, max_length=255)
    expected_size: int = Field(..., ge=1, description="Total file size in bytes")
    content_type: Optional[str] = Field(None, max_length=100, description="Declared Content-Type of the file (optional)")
    sha256: Optional[str] = Field(None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$", description="Hex sha256 of the whole file; verified at completion when given")
