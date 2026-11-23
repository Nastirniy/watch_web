from typing import Dict, List, Optional
from pydantic import Field
from app.backend.utils.validators import ResponseDatetime, ResponseModel


class StatisticsPeriod(ResponseModel):
    date_from: ResponseDatetime
    date_to: ResponseDatetime


class PostStatistics(ResponseModel):
    total: int
    active: int
    online: int = Field(description="Posts with at least one online home user")


class UserStatistics(ResponseModel):
    total: int = Field(description="Not deleted")
    active: int
    online: int
    deleted: int


class ObservationsByPost(ResponseModel):
    post_id: int
    post_name: str
    count: int


class ObservationStatistics(ResponseModel):
    total_in_period: int = Field(description="Created inside the period (created_at), not deleted")
    deleted_in_period: int
    by_status: Dict[str, int]
    by_post: List[ObservationsByPost]


class MediaStatistics(ResponseModel):
    count: int
    total_size_bytes: int
    # Video poster queue by status: pending, processing, ready, failed, untracked
    posters: Dict[str, int] = {}


class StatisticsResponse(ResponseModel):
    generated_at: ResponseDatetime
    period: StatisticsPeriod
    post_ids: Optional[List[int]] = Field(
        default=None,
        description="Posts the figures are limited to; null means every post (statistics.view held at the root)"
    )
    posts: PostStatistics
    users: UserStatistics
    observations: ObservationStatistics
    media: MediaStatistics
