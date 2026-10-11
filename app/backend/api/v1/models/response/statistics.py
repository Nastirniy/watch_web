from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from app.backend.utils.validators import ResponseDatetime


class StatisticsPeriod(BaseModel):
    date_from: ResponseDatetime
    date_to: ResponseDatetime


class PostStatistics(BaseModel):
    total: int
    active: int
    online: int = Field(description="Posts with at least one online home user")


class UserStatistics(BaseModel):
    total: int = Field(description="Not deleted")
    active: int
    online: int
    deleted: int


class ObservationsByPost(BaseModel):
    post_id: int
    post_name: str
    count: int


class ObservationStatistics(BaseModel):
    total_in_period: int = Field(description="Created inside the period (created_at), not deleted")
    deleted_in_period: int
    by_status: Dict[str, int]
    by_post: List[ObservationsByPost]


class MediaStatistics(BaseModel):
    count: int
    total_size_bytes: int
    # Video poster queue by status: pending, processing, ready, failed, untracked
    posters: Dict[str, int] = {}


class StatisticsResponse(BaseModel):
    generated_at: ResponseDatetime
    period: StatisticsPeriod
    post_ids: Optional[List[int]] = Field(
        default=None,
        description="Posts the figures are limited to; null means every post (administrators and post.access_all)"
    )
    posts: PostStatistics
    users: UserStatistics
    observations: ObservationStatistics
    media: MediaStatistics
