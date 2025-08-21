"""
User management schemas for CRUD operations and role-based access control.
Handles user creation, updates, roles, and permissions.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import Field, field_validator, EmailStr

from backend.schemas.base import BaseSchema, TimestampSchema, SearchFilters


class PermissionBase(BaseSchema):
    """Base permission schema."""

    name: str = Field(description="Permission name (e.g., 'read:signals')")
    display_name: str = Field(description="Human-readable permission name")
    description: Optional[str] = Field(None, description="Permission description")
    resource: str = Field(description="Resource type (signals, users, posts, etc.)")
    action: str = Field(description="Action type (create, read, update, delete)")


class PermissionCreate(PermissionBase):
    """Schema for creating permissions."""

    @field_validator('name')
    @classmethod
    def validate_permission_name(cls, v: str) -> str:
        if not v or ':' not in v:
            raise ValueError("Permission name must be in format 'action:resource'")
        return v.lower().strip()


class PermissionUpdate(BaseSchema):
    """Schema for updating permissions."""

    display_name: Optional[str] = Field(None, description="Human-readable permission name")
    description: Optional[str] = Field(None, description="Permission description")


class Permission(PermissionBase, TimestampSchema):
    """Permission response schema."""

    id: int = Field(description="Permission ID")


class RoleBase(BaseSchema):
    """Base role schema."""

    name: str = Field(description="Role name")
    display_name: str = Field(description="Human-readable role name")
    description: Optional[str] = Field(None, description="Role description")
    is_system: bool = Field(default=False, description="System role (cannot be deleted)")
    priority: int = Field(default=0, description="Role priority for hierarchy")


class RoleCreate(RoleBase):
    """Schema for creating roles."""

    permission_ids: List[int] = Field(default_factory=list, description="Permission IDs to assign")

    @field_validator('name')
    @classmethod
    def validate_role_name(cls, v: str) -> str:
        if not v or len(v.strip()) < 2:
            raise ValueError("Role name must be at least 2 characters")
        return v.lower().strip()


class RoleUpdate(BaseSchema):
    """Schema for updating roles."""

    display_name: Optional[str] = Field(None, description="Human-readable role name")
    description: Optional[str] = Field(None, description="Role description")
    priority: Optional[int] = Field(None, description="Role priority")
    permission_ids: Optional[List[int]] = Field(None, description="Permission IDs to assign")


class Role(RoleBase, TimestampSchema):
    """Role response schema."""

    id: int = Field(description="Role ID")
    permissions: List[Permission] = Field(default_factory=list, description="Role permissions")
    user_count: Optional[int] = Field(None, description="Number of users with this role")


class UserBase(BaseSchema):
    """Base user schema."""

    username: str = Field(description="Unique username")
    username_alias: str = Field(description="Display name or alias")
    email: Optional[EmailStr] = Field(None, description="User email address")
    is_active: bool = Field(default=True, description="User active status")
    is_verified: bool = Field(default=False, description="Account verification status")
    post_id: Optional[int] = Field(None, description="Assigned post ID")


class UserCreate(UserBase):
    """Schema for creating users."""

    role_ids: List[int] = Field(default_factory=list, description="Role IDs to assign")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not v or len(v.strip()) < 3:
            raise ValueError("Username must be at least 3 characters")
        if not v.replace('_', '').replace('-', '').replace('.', '').isalnum():
            raise ValueError("Username can only contain letters, numbers, underscores, hyphens, and dots")
        return v.lower().strip()

    @field_validator('username_alias')
    @classmethod
    def validate_alias(cls, v: str) -> str:
        if not v or len(v.strip()) < 2:
            raise ValueError("Username alias must be at least 2 characters")
        return v.strip()


class UserUpdate(BaseSchema):
    """Schema for updating users."""

    username_alias: Optional[str] = Field(None, description="Display name or alias")
    email: Optional[EmailStr] = Field(None, description="User email address")
    is_active: Optional[bool] = Field(None, description="User active status")
    is_verified: Optional[bool] = Field(None, description="Account verification status")
    is_superuser: Optional[bool] = Field(None, description="Superuser privileges")
    post_id: Optional[int] = Field(None, description="Assigned post ID")
    role_ids: Optional[List[int]] = Field(None, description="Role IDs to assign")


class User(UserBase, TimestampSchema):
    """User response schema."""

    id: int = Field(description="User ID")
    pki_certificate_cn: str = Field(description="PKI certificate Common Name")
    pki_certificate_serial: str = Field(description="Certificate serial number")
    pki_certificate_fingerprint: str = Field(description="Certificate fingerprint")
    is_superuser: bool = Field(description="Superuser privileges")
    last_login_at: Optional[datetime] = Field(None, description="Last login timestamp")
    last_record_created_at: Optional[datetime] = Field(None, description="Last record creation timestamp")
    roles: List[Role] = Field(default_factory=list, description="User roles")
    permissions: List[str] = Field(default_factory=list, description="All user permissions")


class UserSummary(BaseSchema):
    """Simplified user schema for listings."""

    id: int = Field(description="User ID")
    username: str = Field(description="Username")
    username_alias: str = Field(description="Display name")
    email: Optional[str] = Field(None, description="Email address")
    is_active: bool = Field(description="User active status")
    is_superuser: bool = Field(description="Superuser status")
    last_login_at: Optional[datetime] = Field(None, description="Last login timestamp")
    role_names: List[str] = Field(default_factory=list, description="Role names")
    post_id: Optional[int] = Field(None, description="Assigned post ID")


class UserSearchFilters(SearchFilters):
    """User search filters."""

    is_active: Optional[bool] = Field(None, description="Filter by active status")
    is_superuser: Optional[bool] = Field(None, description="Filter by superuser status")
    is_verified: Optional[bool] = Field(None, description="Filter by verification status")
    role_ids: Optional[List[int]] = Field(None, description="Filter by role IDs")
    post_id: Optional[int] = Field(None, description="Filter by assigned post")
    has_logged_in: Optional[bool] = Field(None, description="Filter by login history")


class RoleAssignment(BaseSchema):
    """Role assignment schema."""

    user_id: int = Field(description="User ID")
    role_id: int = Field(description="Role ID")
    assigned_by: Optional[int] = Field(None, description="User who assigned the role")


class BulkRoleAssignment(BaseSchema):
    """Bulk role assignment schema."""

    user_ids: List[int] = Field(description="User IDs")
    role_ids: List[int] = Field(description="Role IDs to assign")
    replace_existing: bool = Field(False, description="Replace existing roles")


class PermissionAssignment(BaseSchema):
    """Permission assignment schema."""

    role_id: int = Field(description="Role ID")
    permission_id: int = Field(description="Permission ID")
    granted_by: Optional[int] = Field(None, description="User who granted the permission")


class BulkPermissionAssignment(BaseSchema):
    """Bulk permission assignment schema."""

    role_id: int = Field(description="Role ID")
    permission_ids: List[int] = Field(description="Permission IDs to assign")
    replace_existing: bool = Field(False, description="Replace existing permissions")


class UserStatistics(BaseSchema):
    """User statistics schema."""

    total_users: int = Field(description="Total number of users")
    active_users: int = Field(description="Number of active users")
    verified_users: int = Field(description="Number of verified users")
    superusers: int = Field(description="Number of superusers")
    users_with_posts: int = Field(description="Users assigned to posts")
    recent_logins: int = Field(description="Users with recent logins")
    by_role: dict = Field(default_factory=dict, description="User count by role")


class RoleStatistics(BaseSchema):
    """Role statistics schema."""

    total_roles: int = Field(description="Total number of roles")
    system_roles: int = Field(description="Number of system roles")
    custom_roles: int = Field(description="Number of custom roles")
    roles_with_users: int = Field(description="Roles assigned to users")
    by_permission_count: dict = Field(default_factory=dict, description="Roles by permission count")


class UserActivitySummary(BaseSchema):
    """User activity summary."""

    user_id: int = Field(description="User ID")
    username: str = Field(description="Username")
    last_login_at: Optional[datetime] = Field(None, description="Last login")
    login_count: int = Field(default=0, description="Total login count")
    observation_count: int = Field(default=0, description="Number of observations created")
    last_observation_at: Optional[datetime] = Field(None, description="Last observation timestamp")
    session_count: int = Field(default=0, description="Active session count")


class PasswordPolicy(BaseSchema):
    """Password policy information."""

    min_length: int = Field(8, description="Minimum password length")
    require_uppercase: bool = Field(True, description="Require uppercase letters")
    require_lowercase: bool = Field(True, description="Require lowercase letters")
    require_numbers: bool = Field(True, description="Require numbers")
    require_special: bool = Field(True, description="Require special characters")
    max_age_days: int = Field(90, description="Maximum password age in days")
    history_count: int = Field(5, description="Number of previous passwords to remember")
