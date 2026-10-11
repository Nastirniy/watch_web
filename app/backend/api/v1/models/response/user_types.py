from typing import List

from pydantic import Field

from app.backend.enums.general import UserType
from app.backend.utils.validators import SecureBaseModel


class UserTypeRole(SecureBaseModel):
    id: int
    name: str
    display_name: str


class UserTypeResponse(SecureBaseModel):
    key: UserType
    display_name: str
    description: str
    base_role: UserTypeRole = Field(description="The built-in role of the type: every user of the type gets it by default")
    base_permissions: List[str] = Field(description="Permissions of the base role, sorted")
    allowed_permissions: List[str] = Field(
        description="The ceiling: a role assigned to a user of this type holds nothing outside it. Sorted, includes the base")
