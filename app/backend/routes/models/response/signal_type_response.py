from typing import Optional
from datetime import datetime
from app.backend.utils.validators import SecureBaseModel


class SignalTypeResponse(SecureBaseModel):
    id: int
    code: str
    name: str
    category_id: Optional[int]
    description: Optional[str]
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    is_deleted: bool


class SignalTypeWithCategoryResponse(SecureBaseModel):
    id: int
    code: str
    name: str
    category_id: Optional[int]
    category_name: Optional[str]
    description: Optional[str]
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    is_deleted: bool
