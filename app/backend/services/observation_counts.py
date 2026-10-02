"""Observation counters of one post by created_at (GET /posts/{post_id}/observation-stats).

Aggregates follow the split of GET /statistics: administrators (admin.true plus
observation.view) count any post, everyone else posts of their data scope.
Archived observations are never mixed into not_archived; they are counted
separately on explicit request only. That request needs no observation.archive:
it returns counts, never archived rows.
"""
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import status
from sqlalchemy import case, func, String
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.enums.general import StatsGroupBy
from app.backend.models.observation import Observation
from app.backend.api.shared.models.response.posts_response import (
    ObservationCount, ObservationStatsBucket, ObservationStatsPeriod, PostObservationStatsResponse,
)
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import ErrorCode

LAST_24H = timedelta(hours=24)
DEFAULT_STATS_PERIOD = timedelta(days=30)
MAX_STATS_PERIOD = {StatsGroupBy.HOUR: timedelta(days=31), StatsGroupBy.DAY: timedelta(days=366)}


class utc_hour_key(FunctionElement):
    """'YYYY-MM-DD HH' of a naive UTC datetime column, for grouping by hour in the database"""
    type = String()
    name = "utc_hour_key"
    inherit_cache = True


@compiles(utc_hour_key)
def _utc_hour_key_mysql(element, compiler, **kw):
    column = list(element.clauses)[0]
    return compiler.process(func.date_format(column, "%Y-%m-%d %H"), **kw)


@compiles(utc_hour_key, "sqlite")
def _utc_hour_key_sqlite(element, compiler, **kw):
    column = list(element.clauses)[0]
    return compiler.process(func.strftime("%Y-%m-%d %H", column), **kw)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Naive values are UTC, aware ones are converted"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def check_period(date_from: Optional[datetime], date_to: Optional[datetime], names: str) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ApiError(ErrorCode.PERIOD_REVERSED, fields=names)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ApiError(ErrorCode.UNKNOWN_TIME_ZONE, time_zone=name)


def _bucket_starts(date_from: datetime, date_to: datetime, group_by: StatsGroupBy, zone: ZoneInfo) -> List[datetime]:
    """Bucket starts (UTC) covering the period plus the end of the last bucket.
    Day buckets are calendar days of the zone, so a DST day lasts 23 or 25 hours."""
    if group_by == StatsGroupBy.HOUR:
        current = date_from.replace(minute=0, second=0, microsecond=0)
        starts = []
        while current <= date_to:
            starts.append(current)
            current += timedelta(hours=1)
        return starts + [current]
    day: date = date_from.astimezone(zone).date()
    last_day: date = date_to.astimezone(zone).date()
    starts = []
    while day <= last_day + timedelta(days=1):
        starts.append(datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc))
        day += timedelta(days=1)
    return starts


def _calendar_days_between(earlier: datetime, later: datetime, zone: ZoneInfo) -> int:
    """Calendar days of the zone between two moments: 0 on the same local date, 1 for yesterday"""
    return (later.astimezone(zone).date() - earlier.astimezone(zone).date()).days


def _count_if(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


async def post_observation_stats(
        session: AsyncSession,
        post_id: int,
        date_from: Optional[datetime],
        date_to: Optional[datetime],
        group_by: StatsGroupBy,
        tz: str,
        include_archived: bool,
        now: Optional[datetime] = None,
) -> PostObservationStatsResponse:
    """All-time and last-24-hours figures in one query, then the period series.
    The database groups the period by UTC hour (at most 24 * 366 rows); the hours are
    then summed into buckets of the zone. Exact for zones with whole-hour offsets."""
    zone = _zone(tz)
    now = as_utc(now) or datetime.now(timezone.utc)
    date_to = as_utc(date_to) or now
    date_from = as_utc(date_from) or date_to - DEFAULT_STATS_PERIOD
    check_period(date_from, date_to, "date_from / date_to")
    if date_to - date_from > MAX_STATS_PERIOD[group_by]:
        raise ApiError(
            ErrorCode.PERIOD_TOO_LONG, max_days=MAX_STATS_PERIOD[group_by].days, group_by=group_by.value,
        )
    of_post = [Observation.post_id == post_id]
    if not include_archived:
        of_post.append(Observation.is_deleted == False)

    all_time = {False: 0, True: 0}
    last_24h = {False: 0, True: 0}
    last_created = {False: None, True: None}
    totals = (
        select(
            Observation.is_deleted, func.count(), _count_if(Observation.created_at >= now - LAST_24H),
            func.max(Observation.created_at),
        )
        .where(*of_post)
        .group_by(Observation.is_deleted)
    )
    for is_deleted, count, recent, latest in (await session.exec(totals)).all():
        all_time[bool(is_deleted)] = int(count)
        last_24h[bool(is_deleted)] = int(recent)
        last_created[bool(is_deleted)] = as_utc(latest)
    # Archived observations never count as the latest one
    last_observation_at = last_created[False]

    hour_key = utc_hour_key(Observation.created_at)
    series = (
        select(hour_key, Observation.is_deleted, func.count())
        .where(*of_post, Observation.created_at >= date_from, Observation.created_at <= date_to)
        .group_by(hour_key, Observation.is_deleted)
    )
    edges = _bucket_starts(date_from, date_to, group_by, zone)
    per_bucket = {False: [0] * (len(edges) - 1), True: [0] * (len(edges) - 1)}
    for key, is_deleted, count in (await session.exec(series)).all():
        hour = datetime.strptime(key, "%Y-%m-%d %H").replace(tzinfo=timezone.utc)
        index = _bucket_index(edges, hour)
        if index is not None:
            per_bucket[bool(is_deleted)][index] += int(count)

    def figures(values: dict) -> dict:
        return {"not_archived": values[False], "archived": values[True] if include_archived else None}

    return PostObservationStatsResponse(
        post_id=post_id,
        generated_at=now,
        last_observation_at=last_observation_at,
        days_without_observations=_calendar_days_between(last_observation_at, now, zone) if last_observation_at else None,
        all_time=ObservationCount(**figures(all_time)),
        last_24h=ObservationCount(**figures(last_24h)),
        period=ObservationStatsPeriod(
            date_from=date_from,
            date_to=date_to,
            group_by=group_by.value,
            tz=tz,
            **figures({flag: sum(values) for flag, values in per_bucket.items()}),
            buckets=[
                ObservationStatsBucket(
                    start=edges[i].astimezone(zone),
                    end=edges[i + 1].astimezone(zone),
                    **figures({flag: values[i] for flag, values in per_bucket.items()}),
                )
                for i in range(len(edges) - 1)
            ],
        ),
    )


def _bucket_index(edges: List[datetime], moment: datetime) -> Optional[int]:
    """Index of the bucket holding moment (binary search over the edges)"""
    low, high = 0, len(edges) - 1
    if moment < edges[0] or moment >= edges[-1]:
        return None
    while high - low > 1:
        middle = (low + high) // 2
        if edges[middle] <= moment:
            low = middle
        else:
            high = middle
    return low
