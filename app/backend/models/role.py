from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, Relationship

if TYPE_CHECKING:
    from app.backend.models.user import User


class Role(SQLModel, table=True):
    __tablename__ = "roles"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True, max_length=100)
    display_name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_deleted: bool = Field(default=False, index=True)
    # Built-in roles (one per UserType, owned by code and re-synced at every start) carry the type key here
    builtin_key: Optional[str] = Field(default=None, max_length=50, unique=True, index=True)

    # Relationships
    role_permissions: List["RolePermission"] = Relationship(back_populates="role", cascade_delete=True)
    user_roles: List["UserRole"] = Relationship(back_populates="role", cascade_delete=True)


class Permission(SQLModel, table=True):
    __tablename__ = "permissions"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    display_name: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=1000)

    # Relationships
    role_permissions: List["RolePermission"] = Relationship(back_populates="permission", cascade_delete=True)


class RolePermission(SQLModel, table=True):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    role_id: int = Field(foreign_key="roles.id", index=True)
    permission_id: int = Field(foreign_key="permissions.id", index=True)

    # Relationships
    role: Role = Relationship(back_populates="role_permissions")
    permission: Permission = Relationship(back_populates="role_permissions")


class UserRole(SQLModel, table=True):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    role_id: int = Field(foreign_key="roles.id", index=True)
    assigned_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    user: "User" = Relationship(back_populates="user_roles")
    role: Role = Relationship(back_populates="user_roles")
