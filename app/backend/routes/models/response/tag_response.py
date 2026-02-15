from datetime import datetime
from app.backend.utils.validators import SecureBaseModel


class TagResponse(SecureBaseModel):
    id: int
    name: str
    is_deleted: bool
    created_at: datetime
