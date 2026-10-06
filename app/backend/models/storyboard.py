from typing import Optional
from datetime import datetime
from sqlalchemy import Index
from sqlmodel import Field, SQLModel

STORYBOARD_DIR = "_derived/storyboards"


def storyboard_relative_dir(media_id: int) -> str:
    """Directory of the sheets of one video, relative to the upload root"""
    return f"{STORYBOARD_DIR}/{media_id // 1000:03d}/{media_id}"


class VideoStoryboard(SQLModel, table=True):
    """Storyboard job state and layout of one video (sprite sheets for seek previews)"""
    __tablename__ = "video_storyboards"
    __table_args__ = (Index("ix_video_storyboards_status_locked", "status", "locked_until"),)

    media_id: int = Field(foreign_key="media_files.id", primary_key=True)
    # StoryboardJobStatus
    status: str = Field(max_length=12)
    attempts: int = Field(default=0)
    locked_until: Optional[datetime] = Field(default=None)
    worker: Optional[str] = Field(default=None, max_length=64)
    # For logs and administrators only, never in an API response
    error: Optional[str] = Field(default=None, max_length=500)
    generated_at: Optional[datetime] = Field(default=None)

    # Layout, written on success
    step_seconds: Optional[int] = Field(default=None)
    frame_count: Optional[int] = Field(default=None)
    columns: Optional[int] = Field(default=None)
    rows: Optional[int] = Field(default=None)
    tile_width: Optional[int] = Field(default=None)
    tile_height: Optional[int] = Field(default=None)
    sheet_count: Optional[int] = Field(default=None)
