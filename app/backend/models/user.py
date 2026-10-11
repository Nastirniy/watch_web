from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, Relationship

if TYPE_CHECKING:
    from app.backend.models.post import Post
    from app.backend.models.observation import Observation
    from app.backend.models.role import UserRole
    from app.backend.models.user_post_access import UserPostAccess


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    pki_certificate_cn_hash: str = Field(unique=True, index=True, max_length=255)
    pki_certificate_serial_hash: str = Field(unique=True, max_length=255)
    pki_certificate_fingerprint_hash: str = Field(unique=True, max_length=255)
    username: str = Field(unique=True, index=True, max_length=255)
    username_alias: str = Field(max_length=255)
    is_active: bool = Field(default=True, index=True)
    last_login_at: Optional[datetime] = Field(default=None, index=True)
    post_id: Optional[int] = Field(default=None, foreign_key="posts.id")
    last_record_created_at: Optional[datetime] = Field(default=None)
    is_deleted: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    post: Optional["Post"] = Relationship(back_populates="users")
    sessions: List["UserSession"] = Relationship(back_populates="user", cascade_delete=True)
    user_roles: List["UserRole"] = Relationship(back_populates="user", cascade_delete=True)
    post_access: List["UserPostAccess"] = Relationship(back_populates="user", cascade_delete=True)
    observations: List["Observation"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"foreign_keys": "[Observation.user_id]"}
    )
    disappeared_observations: List["Observation"] = Relationship(
        back_populates="disappeared_by_user",
        sa_relationship_kwargs={"foreign_keys": "[Observation.disappeared_by]"}
    )

    # last_activity_at is a column property attached in models/presence_properties.py
    @property
    def is_online(self) -> bool:
        from app.backend.auth.presence import is_recent
        return is_recent(getattr(self, "last_activity_at", None))


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"

    id: str = Field(primary_key=True, max_length=128)
    user_id: int = Field(foreign_key="users.id", index=True)
    ip_address: Optional[str] = Field(default=None, max_length=45)
    user_agent: Optional[str] = Field(default=None)
    last_activity: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    # When the session ends unless a request comes: every request moves it (auth/sessions.touch)
    expires_at: datetime = Field(index=True)
    # SHA-256 of the client certificate fingerprint the session was opened with (production login through nginx);
    # every request must present the same certificate. Empty for development logins.
    certificate_fingerprint_hash: Optional[str] = Field(default=None, max_length=64)
    # Set when the server knows the session ended. The state (online / idle / ended) is computed when the row is
    # read (auth/sessions.py): a session past expires_at or its idle timeout is ended even before these are set.
    ended_at: Optional[datetime] = Field(default=None, index=True)
    end_reason: Optional[str] = Field(default=None, max_length=20)     # SessionEndReason
    revoke_cause: Optional[str] = Field(default=None, max_length=32)   # SessionRevokeCause, with end_reason revoked
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    user: User = Relationship(back_populates="sessions")
