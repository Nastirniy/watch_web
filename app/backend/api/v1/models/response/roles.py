from typing import Iterable, Optional

from pydantic import Field

from app.backend.bootstrap.permissions import permission_level
from app.backend.enums.general import PermissionLevel, RoleKind
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
        description="Key of the built-in role (GET /user-types names the base roles of each type); null for custom roles")
    is_builtin: bool = Field(
        description="Built-in roles are owned by the system: no rename, no permission change, no archive")
    kind: RoleKind = Field(
        description="tree: holds tree permissions and is assigned at a node; system: holds system permissions and is "
                    "assigned without a node (system assignments). Never changes")


class PermissionResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: str
    level: PermissionLevel = Field(description="tree: acts at a node and below it (tree roles); system: acts on the "
                                                "whole system (system roles)")


def role_to_response(role, permission_names: Iterable[str]) -> RoleResponse:
    """The role row (permission_names is kept for the callers; the kind is stored on the role)"""
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
        kind=RoleKind(role.kind),
    )


def permission_to_response(permission) -> PermissionResponse:
    return PermissionResponse(
        id=permission.id,
        name=permission.name,
        display_name=permission.display_name,
        description=permission.description,
        level=permission_level(permission.name),
    )
