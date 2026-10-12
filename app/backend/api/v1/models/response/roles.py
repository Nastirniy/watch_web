from typing import Iterable, Optional

from pydantic import Field

from app.backend.bootstrap.permissions import permission_level, role_level
from app.backend.enums.general import PermissionLevel
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class RoleResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str]
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    is_deleted: bool = False
    builtin_key: Optional[str] = Field(
        description="Key of the user type this built-in role belongs to; null for custom roles")
    is_builtin: bool = Field(
        description="Built-in roles are owned by the system: no rename, no permission change, no archive")
    level: PermissionLevel = Field(
        description="system when the role holds a system permission (assignable only at the root), else scoped")


class PermissionResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: str
    level: PermissionLevel = Field(description="system: assignable only at the root; scoped: at any node")


def role_to_response(role, permission_names: Iterable[str]) -> RoleResponse:
    """The role row plus the names of its permissions (the level is derived from them)"""
    return RoleResponse(
        id=role.id,
        name=role.name,
        display_name=role.display_name,
        description=role.description,
        created_at=role.created_at,
        updated_at=role.updated_at,
        is_deleted=role.is_deleted,
        builtin_key=role.builtin_key,
        is_builtin=role.builtin_key is not None,
        level=role_level(permission_names),
    )


def permission_to_response(permission) -> PermissionResponse:
    return PermissionResponse(
        id=permission.id,
        name=permission.name,
        display_name=permission.display_name,
        description=permission.description,
        level=permission_level(permission.name),
    )
