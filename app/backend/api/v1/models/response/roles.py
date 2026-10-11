from typing import Optional

from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class RoleResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str]
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    is_deleted: bool = False


class PermissionResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: str
