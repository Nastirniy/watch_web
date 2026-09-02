"""Administration dashboard statistics."""
from typing import Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.enums.general import Permissions
from app.backend.enums.routes import SystemRoutes
from app.backend.models.post import Post
from app.backend.models.user import User
from app.backend.models.observation import Observation
from app.backend.models.media import MediaFile
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.services.presence import online_user_ids_select
from app.backend.routes.models.response.error_response import ACCESS_ERROR_RESPONSES
from app.backend.routes.models.response.statistics_response import (
    StatisticsResponse, StatisticsPeriod, PostStatistics, UserStatistics,
    ObservationStatistics, ObservationsByPost, MediaStatistics,
)

router = APIRouter(responses=ACCESS_ERROR_RESPONSES)

DEFAULT_PERIOD_DAYS = 30


async def _scalar(session: AsyncSession, query) -> int:
    value = (await session.exec(query)).one()
    return int(value or 0)


@router.get(SystemRoutes.ROOT.value, response_model=StatisticsResponse)
async def get_statistics(
        date_from: Optional[datetime] = Query(None, description="Period start (default: date_to minus 30 days)"),
        date_to: Optional[datetime] = Query(None, description="Period end (default: now)"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.STATISTICS_VIEW], admin=True))
):
    """Application statistics for the administration dashboard"""
    now = datetime.now(timezone.utc)
    date_to = date_to or now
    date_from = date_from or date_to - timedelta(days=DEFAULT_PERIOD_DAYS)
    if date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=timezone.utc)
    if date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=timezone.utc)
    if date_from > date_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date_from must not be after date_to")

    posts = PostStatistics(
        total=await _scalar(session, select(func.count()).select_from(Post)),
        active=await _scalar(session, select(func.count()).select_from(Post).where(Post.is_active == True)),
        online=await _scalar(session, select(func.count()).select_from(Post).where(Post.online_user_count > 0)),
    )

    online_ids = online_user_ids_select()
    users = UserStatistics(
        total=await _scalar(session, select(func.count()).select_from(User).where(User.is_deleted == False)),
        active=await _scalar(session, select(func.count()).select_from(User).where(User.is_deleted == False, User.is_active == True)),
        online=await _scalar(session, select(func.count()).select_from(User).where(User.is_deleted == False, User.id.in_(online_ids))),
        deleted=await _scalar(session, select(func.count()).select_from(User).where(User.is_deleted == True)),
    )

    in_period = (Observation.observed_at >= date_from, Observation.observed_at <= date_to)
    status_rows = (await session.exec(
        select(Observation.status, func.count())
        .where(*in_period, Observation.is_deleted == False)
        .group_by(Observation.status)
    )).all()
    post_rows = (await session.exec(
        select(Post.id, Post.name, func.count(Observation.id))
        .join(Observation, Observation.post_id == Post.id)
        .where(*in_period, Observation.is_deleted == False)
        .group_by(Post.id, Post.name)
        .order_by(func.count(Observation.id).desc())
    )).all()
    observations = ObservationStatistics(
        total_in_period=sum(count for _, count in status_rows),
        deleted_in_period=await _scalar(session, select(func.count()).select_from(Observation).where(*in_period, Observation.is_deleted == True)),
        by_status={status_value: int(count) for status_value, count in status_rows},
        by_post=[ObservationsByPost(post_id=post_id, post_name=name, count=int(count)) for post_id, name, count in post_rows],
    )

    media_count, media_size = (await session.exec(
        select(func.count(), func.coalesce(func.sum(MediaFile.file_size), 0)).where(MediaFile.is_deleted == False)
    )).one()
    media = MediaStatistics(count=int(media_count or 0), total_size_bytes=int(media_size or 0))

    return StatisticsResponse(
        generated_at=now,
        period=StatisticsPeriod(date_from=date_from, date_to=date_to),
        posts=posts,
        users=users,
        observations=observations,
        media=media,
    )
