from typing import Dict, Optional

from pydantic import BaseModel

from app.backend.api.shared.openapi_docs import header
from app.backend.utils.validators import ResponseDatetime


class UploadSessionResponse(BaseModel):
    """State of a resumable upload; received_bytes is the offset to continue from"""
    id: str
    status: str                                   # open | completed | aborted | expired
    observation_id: int
    media_type: str
    original_filename: str
    expected_size: int
    received_bytes: int
    remaining_bytes: int
    max_chunk_bytes: int
    media_file_id: Optional[int] = None
    error: Optional[str] = None
    created_at: ResponseDatetime
    last_activity_at: ResponseDatetime
    expires_at: ResponseDatetime
    completed_at: Optional[ResponseDatetime] = None
    urls: Dict[str, str]


# Headers of the upload session routes
UPLOAD_SESSION_HEADERS = {
    "Upload-Offset": header("Bytes received so far: send the next chunk from here", "integer"),
    "Upload-Length": header("expected_size of the session", "integer"),
    "Upload-Expires": header("When the session expires", "string", "date-time"),
    "Upload-Status": header("open, completed, aborted or expired"),
    "Cache-Control": header("no-store"),
}
