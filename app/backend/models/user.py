from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, Relationship

if TYPE_CHECKING:
    from app.backend.models.post import Post
    from app.backend.models.observation import Observation
    from app.backend.models.role_assignment import RoleAssignment


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    # The certificates a user logs in with are rows of user_certificates (models/user_certificate.py, migration 024)
    username: str = Field(unique=True, index=True, max_length=255)
    username_alias: str = Field(max_length=255)
    is_active: bool = Field(default=True, index=True)
    # last_login_at is derived from the login sessions (presence_properties.py): a request never writes this row on a
    # hot path, or two requests of one user collide on it (migration 026)
    # Home post: membership only (which delegated administrators manage the user), grants nothing by itself
    post_id: Optional[int] = Field(default=None, foreign_key="posts.id")
    # UserType: the pages of the web client and the ceiling of the roles the user may hold; no DB CHECK, a new type
    # is a new enum value
    user_type: str = Field(max_length=32)
    is_deleted: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    post: Optional["Post"] = Relationship(back_populates="users")
    sessions: List["UserSession"] = Relationship(back_populates="user", cascade_delete=True)
    assignments: List["RoleAssignment"] = Relationship(back_populates="user", cascade_delete=True)
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
    user_agent: Optional[str] = Field(default=None, max_length=255)  # VARCHAR(255) on MySQL already
    last_activity: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    # When the session ends unless a request comes: every request moves it (auth/sessions.touch)
    expires_at: datetime = Field(index=True)
    # SHA-256 of the client certificate fingerprint the session was opened with (production login through nginx);
    # every request must present the same certificate. Empty for development logins.
    certificate_fingerprint_hash: Optional[str] = Field(default=None, max_length=64)
    # jti of the one refresh token that may refresh the session; every refresh replaces it (auth/sessions.py,
    # claim_refresh). Empty only for sessions opened before migration 025: their first refresh sets it.
    refresh_jti: Optional[str] = Field(default=None, max_length=64)
    # The token before it and when the last refresh replaced it (claim_refresh, migration 034): a used token presented
    # again ends the session, except this one within security.refresh_reuse_grace_seconds of the rotation
    # (auth/sessions.py refresh_reuse). Empty for a session that has not refreshed since 034.
    previous_refresh_jti: Optional[str] = Field(default=None, max_length=64)
    refresh_rotated_at: Optional[datetime] = Field(default=None)
    # Set when the server knows the session ended. The state (online / idle / ended) is computed when the row is
    # read (auth/sessions.py): a session past expires_at or its idle timeout is ended even before these are set.
    ended_at: Optional[datetime] = Field(default=None, index=True)
    end_reason: Optional[str] = Field(default=None, max_length=20)     # SessionEndReason
    revoke_cause: Optional[str] = Field(default=None, max_length=32)   # SessionRevokeCause, with end_reason revoked
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    user: User = Relationship(back_populates="sessions")
