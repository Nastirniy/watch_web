from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, Relationship
from app.backend.enums.general import ConnectionStatus

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
    last_connection_at: Optional[datetime] = Field(default=None, index=True)
    connection_status: str = Field(
        default=ConnectionStatus.OFFLINE.value,
        index=True,
        max_length=20
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    users: List["User"] = Relationship(back_populates="post")
    observations: List["Observation"] = Relationship(back_populates="post")
    user_access: List["UserPostAccess"] = Relationship(back_populates="post")

    # online_user_count and last_user_activity_at are column properties attached in models/presence_properties.py
    @property
    def effective_connection_status(self) -> str:
        """online while any home user is online, otherwise the stored status"""
        if (getattr(self, "online_user_count", 0) or 0) > 0:
            return ConnectionStatus.ONLINE.value
        return self.connection_status

    @property
    def effective_last_connection_at(self) -> Optional[datetime]:
        candidates = [v for v in (self.last_connection_at, getattr(self, "last_user_activity_at", None)) if v is not None]
        if not candidates:
            return None
        return max(v if v.tzinfo else v.replace(tzinfo=timezone.utc) for v in candidates)
