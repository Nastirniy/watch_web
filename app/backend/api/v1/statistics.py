"""Dashboard statistics.

Administrators (admin.true) see figures over every post. Everyone else holding
statistics.view sees them over their data scope (home post, user_post_access
rows, or every post with post.access_all): posts in the scope, users whose home
post is in the scope, observations and media of those posts.
"""
from typing import Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.enums.general import Permissions
from app.backend.enums.routes import StatisticsRoutes
from app.backend.models.post import Post
from app.backend.models.user import User
from app.backend.models.observation import Observation
from app.backend.models.media import MediaFile
from app.backend.services.media.jobs import poster_counts
from app.backend.auth.context import AuthContext
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.require import require
from app.backend.auth.presence import online_user_ids_select
from app.backend.api.v1.models.response.statistics import (
    StatisticsResponse,
    StatisticsPeriod,
    PostStatistics,
    UserStatistics,
    ObservationStatistics,
    ObservationsByPost,
    MediaStatistics,
)
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import ErrorCode
from app.backend.errors.openapi import GUARDED, problems
from app.backend.utils.validators import UtcDatetime

router = APIRouter(prefix=StatisticsRoutes.BASE.value, tags=[StatisticsRoutes.tag()])

DEFAULT_PERIOD_DAYS = 30


async def _scalar(session: AsyncSession, query) -> int:
    value = (await session.exec(query)).one()
    return int(value or 0)


@router.get(
    StatisticsRoutes.ROOT.value,
    response_model=StatisticsResponse,
    responses=problems(ErrorCode.PERIOD_REVERSED, *GUARDED),
)
async def get_statistics(
        date_from: Optional[UtcDatetime] = Query(None, description="Period start, observations by created_at (default: date_to minus 30 days)"),
        date_to: Optional[UtcDatetime] = Query(None, description="Period end, observations by created_at (default: now)"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.STATISTICS_VIEW]))
):
    """Application statistics: every post for administrators, the caller's post scope for everyone else"""
    now = datetime.now(timezone.utc)
    date_to = date_to or now
    date_from = date_from or date_to - timedelta(days=DEFAULT_PERIOD_DAYS)
    if date_from > date_to:
        raise ApiError(ErrorCode.PERIOD_REVERSED, fields="date_from / date_to")

    scope = None if auth.is_admin else AccessControlService.post_scope(auth)
    post_ids = sorted(scope) if scope is not None else None
    # Scope conditions; an empty scope matches nothing
    in_posts = (Post.id.in_(post_ids),) if post_ids is not None else ()
    in_user_posts = (User.post_id.in_(post_ids),) if post_ids is not None else ()
    in_observation_posts = (Observation.post_id.in_(post_ids),) if post_ids is not None else ()

    posts = PostStatistics(
        total=await _scalar(session, select(func.count()).select_from(Post).where(*in_posts)),
        active=await _scalar(session, select(func.count()).select_from(Post).where(*in_posts, Post.is_active == True)),
        online=await _scalar(session, select(func.count()).select_from(Post).where(*in_posts, Post.online_user_count > 0)),
    )

    online_ids = online_user_ids_select()
    users = UserStatistics(
        total=await _scalar(session, select(func.count()).select_from(User).where(*in_user_posts, User.is_deleted == False)),
        active=await _scalar(session, select(func.count()).select_from(User).where(*in_user_posts, User.is_deleted == False, User.is_active == True)),
        online=await _scalar(session, select(func.count()).select_from(User).where(*in_user_posts, User.is_deleted == False, User.id.in_(online_ids))),
        deleted=await _scalar(session, select(func.count()).select_from(User).where(*in_user_posts, User.is_deleted == True)),
    )

    # Observations count by created_at (when they were recorded), not observed_at
    in_period = (Observation.created_at >= date_from, Observation.created_at <= date_to, *in_observation_posts)
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

    media_query = select(func.count(), func.coalesce(func.sum(MediaFile.file_size), 0)).where(MediaFile.is_deleted == False)
    if post_ids is not None:
        media_query = media_query.join(Observation, Observation.id == MediaFile.observation_id).where(*in_observation_posts)
    media_count, media_size = (await session.exec(media_query)).one()
    media = MediaStatistics(
        count=int(media_count or 0),
        total_size_bytes=int(media_size or 0),
        posters=await poster_counts(session, post_ids),
    )

    return StatisticsResponse(
        generated_at=now,
        period=StatisticsPeriod(date_from=date_from, date_to=date_to),
        post_ids=post_ids,
        posts=posts,
        users=users,
        observations=observations,
        media=media,
    )
