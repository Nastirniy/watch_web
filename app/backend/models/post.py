from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from sqlalchemy import false
from sqlmodel import Field, SQLModel, Relationship

if TYPE_CHECKING:
    from app.backend.models.observation import Observation
    from app.backend.models.user import User
    from app.backend.models.user_post_access import UserPostAccess


class Post(SQLModel, table=True):
    __tablename__ = "posts"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)
    code: str = Field(unique=True, index=True, max_length=50)
    description: Optional[str] = Field(default=None, max_length=1000)
    region: Optional[str] = Field(default=None, max_length=255)
    city: Optional[str] = Field(default=None, max_length=255)
    is_active: bool = Field(default=True, index=True)
    # Archived (POST /posts/{id}/archive): leaves every home-post and access-row data scope, its data is read-only.
    # NOT NULL with a server default like the column 020 adds, so a fresh install (built by 002) gets the same column
    is_deleted: bool = Field(default=False, index=True, sa_column_kwargs={"server_default": false(), "nullable": False})
    # Place in the scope tree (scope_groups); null: directly under the root
    group_id: Optional[int] = Field(default=None, foreign_key="scope_groups.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    users: List["User"] = Relationship(back_populates="post")
    observations: List["Observation"] = Relationship(back_populates="post")
    user_access: List["UserPostAccess"] = Relationship(back_populates="post")

    # online_user_count, user_count and last_user_activity_at are column properties attached in models/presence_properties.py
