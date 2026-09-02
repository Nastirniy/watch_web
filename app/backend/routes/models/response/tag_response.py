from datetime import datetime
from app.backend.utils.validators import SecureBaseModel


class TagBriefResponse(SecureBaseModel):
    """Tag as embedded in observations (deleted tags stay attached for history)"""
    id: int
    name: str
    is_deleted: bool


class TagResponse(SecureBaseModel):
    id: int
    name: str
    is_deleted: bool
    created_at: datetime
