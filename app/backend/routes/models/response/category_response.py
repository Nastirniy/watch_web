from typing import List
from app.backend.utils.validators import SecureBaseModel


class CategoryResponse(SecureBaseModel):
    id: int
    name: str
    alias: str
    is_deleted: bool


class CategoryWithSignalTypesResponse(SecureBaseModel):
    id: int
    name: str
    alias: str
    is_deleted: bool
    signal_type_ids: List[int]
