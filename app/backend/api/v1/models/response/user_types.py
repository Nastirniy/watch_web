from typing import List

from pydantic import Field

from app.backend.enums.general import RoleKind, UserType
from app.backend.utils.validators import SecureBaseModel


class UserTypeBaseRole(SecureBaseModel):
    """A built-in role every new user of the type gets"""
    id: int
    name: str
    display_name: str
    kind: RoleKind = Field(description="tree: given at the home node (or the root); system: a system assignment")
    at_root: bool = Field(description="A tree base role given at the root whatever the home post")


class UserTypeResponse(SecureBaseModel):
    key: UserType
    display_name: str
    description: str
    base_roles: List[UserTypeBaseRole] = Field(description="The built-in roles a new user of the type gets")
    tree_boundary: List[str] = Field(
        description="The most a user of the type can ever have in effect through tree roles, sorted: a tree role "
                    "outside it is refused (role_exceeds_user_type) and its excess never acts")
    system_boundary: List[str] = Field(
        description="The same for system roles, sorted (empty: no system role for this type)")
