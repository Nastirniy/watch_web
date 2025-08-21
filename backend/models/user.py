"""
User, Role, Permission, and Session models.
Handles authentication, authorization, and PKI certificate management.
"""

from datetime import datetime
from typing import List, Optional, TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

from backend.models.base import BaseModel, StatusMixin

if TYPE_CHECKING:
    from backend.models.post import Post
    from backend.models.signal import Observation
    from backend.models.export import ExportJob
    from backend.models.audit import AuditLog


# Many-to-many relationship table for users and roles
class UserRole(SQLModel, table=True):
    """Association table for user-role relationships."""

    __tablename__ = "user_roles"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    role_id: int = Field(foreign_key="roles.id", primary_key=True)
    assigned_at: datetime = Field(default_factory=datetime.utcnow)
    assigned_by: Optional[int] = Field(foreign_key="users.id", default=None)


class Permission(BaseModel, table=True):
    """System permissions for role-based access control."""

    __tablename__ = "permissions"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=100, unique=True, index=True, description="Permission name (e.g., 'read:signals')")
    display_name: str = Field(max_length=255, description="Human-readable permission name")
    description: Optional[str] = Field(default=None, description="Permission description")
    resource: str = Field(max_length=50, description="Resource type (signals, users, posts, etc.)")
    action: str = Field(max_length=50, description="Action type (create, read, update, delete)")

    # Relationships
    roles: List["Role"] = Relationship(back_populates="permissions", link_model="RolePermission")


class Role(BaseModel, table=True):
    """User roles with associated permissions."""

    __tablename__ = "roles"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=100, unique=True, index=True, description="Role name")
    display_name: str = Field(max_length=255, description="Human-readable role name")
    description: Optional[str] = Field(default=None, description="Role description")
    is_system: bool = Field(default=False, description="System role (cannot be deleted)")
    priority: int = Field(default=0, description="Role priority for hierarchy")

    # Relationships
    users: List["User"] = Relationship(back_populates="roles", link_model=UserRole)
    permissions: List[Permission] = Relationship(back_populates="roles", link_model="RolePermission")


# Many-to-many relationship table for roles and permissions
class RolePermission(SQLModel, table=True):
    """Association table for role-permission relationships."""

    __tablename__ = "role_permissions"

    role_id: int = Field(foreign_key="roles.id", primary_key=True)
    permission_id: int = Field(foreign_key="permissions.id", primary_key=True)
    granted_at: datetime = Field(default_factory=datetime.utcnow)
    granted_by: Optional[int] = Field(foreign_key="users.id", default=None)


class User(BaseModel, StatusMixin, table=True):
    """User model with PKI certificate authentication."""

    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)

    # PKI Certificate fields
    pki_certificate_cn: str = Field(
        max_length=255,
        unique=True,
        index=True,
        description="Common Name from PKI certificate"
    )
    pki_certificate_serial: str = Field(
        max_length=255,
        unique=True,
        description="Certificate serial number"
    )
    pki_certificate_fingerprint: str = Field(
        max_length=255,
        unique=True,
        description="Certificate fingerprint"
    )

    # User information
    username: str = Field(max_length=255, unique=True, index=True, description="Unique username")
    username_alias: str = Field(max_length=255, description="Display name or alias")
    email: Optional[str] = Field(default=None, max_length=255, description="User email address")

    # Authentication and session tracking
    last_login_at: Optional[datetime] = Field(default=None, index=True, description="Last login timestamp")
    last_record_created_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of last record creation"
    )

    # User settings and status
    is_superuser: bool = Field(default=False, description="Superuser privileges")
    is_verified: bool = Field(default=False, description="Account verification status")

    # Post assignment (if user is assigned to a specific post)
    post_id: Optional[int] = Field(default=None, foreign_key="posts.id", description="Assigned post ID")

    # Relationships
    post: Optional["Post"] = Relationship(back_populates="users")
    roles: List[Role] = Relationship(back_populates="users", link_model=UserRole)
    sessions: List["UserSession"] = Relationship(back_populates="user", cascade_delete=True)
    observations: List["Observation"] = Relationship(back_populates="user")
    export_jobs: List["ExportJob"] = Relationship(back_populates="user", cascade_delete=True)
    audit_logs: List["AuditLog"] = Relationship(back_populates="user")

    def has_permission(self, permission_name: str) -> bool:
        """Check if user has a specific permission."""
        if self.is_superuser:
            return True

        for role in self.roles:
            for perm in role.permissions:
                if perm.name == permission_name:
                    return True
        return False

    def has_role(self, role_name: str) -> bool:
        """Check if user has a specific role."""
        if self.is_superuser:
            return True

        return any(role.name == role_name for role in self.roles)

    def get_permissions(self) -> List[str]:
        """Get all user permissions."""
        if self.is_superuser:
            return ["*"]  # Superuser has all permissions

        permissions = set()
        for role in self.roles:
            for perm in role.permissions:
                permissions.add(perm.name)
        return list(permissions)


class UserSession(BaseModel, table=True):
    """User session tracking for authentication and audit."""

    __tablename__ = "user_sessions"

    id: str = Field(max_length=128, primary_key=True, description="Session ID")
    user_id: int = Field(foreign_key="users.id", index=True, description="User ID")

    # Session metadata
    ip_address: Optional[str] = Field(default=None, max_length=45, description="Client IP address")
    user_agent: Optional[str] = Field(default=None, description="Client user agent")

    # Session timing
    last_activity: datetime = Field(
        default_factory=datetime.utcnow,
        index=True,
        description="Last activity timestamp"
    )
    expires_at: datetime = Field(index=True, description="Session expiration time")

    # Session status
    is_active: bool = Field(default=True, description="Session active status")
    logout_reason: Optional[str] = Field(default=None, description="Logout reason if applicable")

    # Relationships
    user: User = Relationship(back_populates="sessions")

    @property
    def is_expired(self) -> bool:
        """Check if session is expired."""
        return datetime.utcnow() > self.expires_at

    def refresh(self, expire_minutes: int = 30) -> None:
        """Refresh session expiration time."""
        from datetime import timedelta
        self.last_activity = datetime.utcnow()
        self.expires_at = datetime.utcnow() + timedelta(minutes=expire_minutes)
