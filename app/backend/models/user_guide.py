from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, Index, false
from sqlmodel import Field, SQLModel


class UserGuide(SQLModel, table=True):
    """A user guide: a titled entry of the help section with its files (UserGuideFile), one per language or format.

    Reading is open to every authenticated user; writes need the system permissions guide.create / guide.update /
    guide.archive. audience_key null is everyone, else the scope key of a node ("g:<id>" / "p:<id>") whose users see
    the guide. description is plain text. Archived (is_deleted), never deleted. Created by migration 033_user_guides.
    """
    __tablename__ = "user_guides"
    __table_args__ = (
        # The list every reader opens: active guides (is_deleted = false) in their order (sort_order, then title);
        # the index serves the filter and the ORDER BY at once
        Index("ix_user_guides_list", "is_deleted", "sort_order", "title"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    sort_order: int = Field(default=0, sa_column_kwargs={"server_default": "0", "nullable": False})
    audience_key: Optional[str] = Field(default=None, max_length=24)
    is_deleted: bool = Field(default=False, sa_column_kwargs={"server_default": false(), "nullable": False})
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # who acted: no foreign key (migration 028); null for rows written without a user
    created_by: Optional[int] = Field(default=None)
    updated_by: Optional[int] = Field(default=None)


class UserGuideFile(SQLModel, table=True):
    """A file of a user guide, stored at <upload_path>/<file_path> (_guides/<guide_id>/<file_id>/<stored_name>).

    file_name is the sanitised original name, stored_name the uuid hex plus extension, format_id the media_formats
    row the upload matched (formats are archived, never deleted: no foreign key). Archiving a file keeps it on disk
    (history); nothing is ever deleted. Created by migration 033_user_guides.
    """
    __tablename__ = "user_guide_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    # The files of one guide (the guide page lists them): ix_user_guide_files_guide_id; on MariaDB it is also the
    # index the foreign key needs
    guide_id: int = Field(foreign_key="user_guides.id", index=True)
    file_name: str = Field(max_length=255)
    stored_name: str = Field(max_length=64)
    file_path: str = Field(max_length=500)
    content_type: str = Field(max_length=100)
    format_id: int = Field()
    size_bytes: int = Field(sa_column=Column(BigInteger, nullable=False))
    sha256: str = Field(max_length=64)
    language: Optional[str] = Field(default=None, max_length=8)
    is_deleted: bool = Field(default=False, sa_column_kwargs={"server_default": false(), "nullable": False})
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Optional[int] = Field(default=None)   # who acted: no foreign key (migration 028)
