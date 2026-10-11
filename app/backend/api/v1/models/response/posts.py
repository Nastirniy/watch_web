from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field

from app.backend.models.post import Post
from app.backend.utils.validators import PlainTextModel, ResponseDatetime


class PostResponse(PlainTextModel):
    """Post row plus presence of its home users (users whose users.post_id is the post)"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    description: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    is_active: bool
    online_user_count: int = Field(default=0, description="Home users with an authenticated request within security.online_threshold_seconds")
    last_user_activity_at: Optional[ResponseDatetime] = Field(default=None, description="Latest activity of any home user; null when none has an active session")
    user_count: int = Field(default=0, description="Active, not archived users whose home post is this post")
    created_at: ResponseDatetime
    updated_at: ResponseDatetime


def post_to_response(post: Post) -> PostResponse:
    return PostResponse(
        id=post.id,
        name=post.name,
        code=post.code,
        description=post.description,
        region=post.region,
        city=post.city,
        is_active=post.is_active,
        online_user_count=post.online_user_count or 0,
        last_user_activity_at=post.last_user_activity_at,
        user_count=post.user_count or 0,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )



class ObservationCount(BaseModel):
    """Observations by created_at. The two figures never overlap: all = not_archived + archived."""
    not_archived: int = Field(description="Observations that are not archived")
    archived: Optional[int] = Field(default=None, description="Archived observations; null unless include_archived")


class ObservationStatsBucket(ObservationCount):
    start: ResponseDatetime = Field(description="Bucket start in the requested time zone")
    end: ResponseDatetime


class ObservationStatsPeriod(ObservationCount):
    date_from: ResponseDatetime
    date_to: ResponseDatetime
    group_by: str
    tz: str
    buckets: List[ObservationStatsBucket] = Field(description="Every bucket of the period in order, empty ones included")


class PostObservationStatsResponse(BaseModel):
    post_id: int
    generated_at: ResponseDatetime
    last_observation_at: Optional[ResponseDatetime] = Field(default=None, description="created_at of the latest not archived observation; null when there is none")
    days_without_observations: Optional[int] = Field(
        default=None,
        description="Calendar days in tz from the date of last_observation_at to the date of generated_at: 0 today, 1 yesterday; null when there is no observation",
    )
    all_time: ObservationCount
    last_24h: ObservationCount = Field(description="Created within the 24 hours before generated_at")
    period: ObservationStatsPeriod
