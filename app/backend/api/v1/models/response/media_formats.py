from typing import List

from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class MediaFormatResponse(SecureBaseModel):
    id: int
    media_type: str
    content_type: str
    label: str
    extensions: List[str]
    aliases: List[str]
    max_file_size_bytes: int
    is_active: bool
    sort_order: int
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
