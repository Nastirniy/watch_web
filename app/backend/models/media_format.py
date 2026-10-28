from typing import List, Optional
from datetime import datetime
from sqlalchemy import BigInteger, Column, JSON
from sqlmodel import Field, SQLModel
from app.backend.utils.datetime_utils import get_default_timezone_datetime


class MediaFormat(SQLModel, table=True):
    """An accepted upload format, managed through /media-formats (administration).

    Uploads are checked against the active, non-deleted rows: declared
    Content-Type, file extension, detected MIME type and file size. Defaults
    are seeded from DEFAULT_FORMATS in services/files/formats.py.
    """
    __tablename__ = "media_formats"

    id: Optional[int] = Field(default=None, primary_key=True)
    media_type: str = Field(max_length=10, index=True)            # image | video | audio | document
    content_type: str = Field(max_length=100, unique=True)        # canonical Content-Type, lower case
    label: str = Field(max_length=255)
    extensions: List[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))   # [".jpg", ".jpeg"]
    aliases: List[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))      # other MIME spellings detectors produce
    max_file_size_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    is_active: bool = Field(default=True, index=True)
    sort_order: int = Field(default=0)
    is_deleted: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=get_default_timezone_datetime)
    updated_at: datetime = Field(default_factory=get_default_timezone_datetime)
