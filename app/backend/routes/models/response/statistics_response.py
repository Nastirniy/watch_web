from typing import Dict, List
from datetime import datetime
from pydantic import BaseModel, Field


class StatisticsPeriod(BaseModel):
    date_from: datetime
    date_to: datetime


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
    total_in_period: int = Field(description="Observed inside the period, not deleted")
    deleted_in_period: int
    by_status: Dict[str, int]
    by_post: List[ObservationsByPost]


class MediaStatistics(BaseModel):
    count: int
    total_size_bytes: int


class StatisticsResponse(BaseModel):
    generated_at: datetime
    period: StatisticsPeriod
    posts: PostStatistics
    users: UserStatistics
    observations: ObservationStatistics
    media: MediaStatistics
