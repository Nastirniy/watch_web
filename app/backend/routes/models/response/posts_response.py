from typing import Optional
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

from app.backend.models.post import Post


class PostResponse(BaseModel):
    """Post row plus presence derived from its home users.

    connection_status is 'online' while at least one home user is online,
    otherwise the stored value (offline, maintenance, error).
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    description: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    is_active: bool
    connection_status: str = Field(description="Effective status: online when any home user is online")
    stored_connection_status: str = Field(description="Value stored on the post (offline, maintenance, error)")
    last_connection_at: Optional[datetime] = Field(default=None, description="Latest of stored value and home users' activity")
    online_user_count: int = 0
    created_at: datetime
    updated_at: datetime


def post_to_response(post: Post) -> PostResponse:
    return PostResponse(
        id=post.id,
        name=post.name,
        code=post.code,
        description=post.description,
        region=post.region,
        city=post.city,
        is_active=post.is_active,
        connection_status=post.effective_connection_status,
        stored_connection_status=post.connection_status,
        last_connection_at=post.effective_last_connection_at,
        online_user_count=post.online_user_count or 0,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )
