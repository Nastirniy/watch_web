from typing import Optional
from datetime import datetime

from app.backend.utils.validators import SecureBaseModel


class RoleResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime


class PermissionResponse(SecureBaseModel):
    id: int
    name: str
    display_name: str
    description: str
