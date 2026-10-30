from typing import Optional
from datetime import datetime
from sqlalchemy import BigInteger, Column
from sqlmodel import Field, SQLModel
from app.backend.utils.datetime_utils import get_default_timezone_datetime


class UploadSession(SQLModel, table=True):
    """A resumable upload in progress (POST /media/uploads).

    The client sends the file in chunks at explicit offsets; received_bytes is
    the only truth about progress and is what the client asks for after a
    dropped connection. The part file lives under <upload_path>/_tmp/uploads
    until completion moves it into storage as a MediaFile.
    """
    __tablename__ = "upload_sessions"

    id: str = Field(primary_key=True, max_length=32)              # uuid4 hex
    user_id: int = Field(foreign_key="users.id", index=True)
    observation_id: int = Field(foreign_key="observations.id", index=True)
    post_id: int = Field(index=True)                               # denormalised for scope checks

    media_type: str = Field(max_length=10)
    original_filename: str = Field(max_length=255)
    declared_content_type: Optional[str] = Field(default=None, max_length=100)
    expected_size: int = Field(sa_column=Column(BigInteger, nullable=False))
    received_bytes: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    expected_sha256: Optional[str] = Field(default=None, max_length=64)

    status: str = Field(default="open", max_length=12, index=True)   # open | completing | completed | aborted | expired
    media_file_id: Optional[int] = Field(default=None, foreign_key="media_files.id")
    part_path: str = Field(max_length=500)                          # relative to the upload root
    error: Optional[str] = Field(default=None, max_length=500)       # problem code that aborted the session

    # One request at a time: token held while a PATCH streams into the part file or a complete stores it
    # (status completing); writer_since is the time of the claim or of its last renewal
    writer_token: Optional[str] = Field(default=None, max_length=32)
    writer_since: Optional[datetime] = Field(default=None)
    # The last time bytes of a chunk arrived (first byte of a chunk, every renewal while it streams, the stored chunk);
    # a claim or a failed attempt without bytes does not move it. Null until the first chunk. A waiting move reads it
    last_chunk_at: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=get_default_timezone_datetime)
    updated_at: datetime = Field(default_factory=get_default_timezone_datetime)
    last_activity_at: datetime = Field(default_factory=get_default_timezone_datetime)
    expires_at: datetime = Field(index=True)
    completed_at: Optional[datetime] = Field(default=None)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def remaining_bytes(self) -> int:
        return max(0, int(self.expected_size) - int(self.received_bytes))
