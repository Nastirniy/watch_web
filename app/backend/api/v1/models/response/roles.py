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
        default=None, description="Key of the user type this built-in role belongs to; null for custom roles")
    is_builtin: bool = Field(
        default=False, description="Built-in roles are owned by the system: no rename, no permission change, no archive")
    level: PermissionLevel = Field(
        default=PermissionLevel.SCOPED,
        description="system when the role holds a system permission (assignable only at the root), else scoped")


class PermissionResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: str
    level: PermissionLevel = Field(
        default=PermissionLevel.SCOPED, description="system: assignable only at the root; scoped: at any node")


def role_to_response(role, permission_names: Iterable[str]) -> RoleResponse:
    """The role row plus the names of its permissions (the level is derived from them)"""
    response = RoleResponse.model_validate(role)
    response.is_builtin = role.builtin_key is not None
    response.level = role_level(permission_names)
    return response


def permission_to_response(permission) -> PermissionResponse:
    response = PermissionResponse.model_validate(permission)
    response.level = permission_level(permission.name)
    return response
