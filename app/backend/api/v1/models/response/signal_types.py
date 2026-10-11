from typing import Optional
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class SignalTypeResponse(SecureBaseModel):
    id: int
    code: str
    name: str
    category_id: Optional[int]
    description: Optional[str]
    is_active: bool
    sort_order: int
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
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
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    is_deleted: bool
