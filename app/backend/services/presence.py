"""User presence (online / offline).

A user is online when one of their active sessions was touched within
security.online_threshold_seconds. Sessions are touched by
dependencies.get_current_user on every authenticated request, at most once
per security.activity_touch_interval_seconds.

Presence is exposed through:
- User.last_activity_at (column property, max last_activity of active sessions)
- User.is_online (property comparing last_activity_at with the cutoff)
- Post.online_user_count / Post.last_user_activity_at (column properties)
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import bindparam, select as sa_select

from app.backend.config import settings


def online_threshold_seconds() -> int:
    return int(settings.security.online_threshold_seconds)


def activity_touch_interval_seconds() -> int:
    return int(settings.security.activity_touch_interval_seconds)


def online_cutoff() -> datetime:
    """Aware UTC timestamp; sessions touched at or after it count as online."""
    return datetime.now(timezone.utc) - timedelta(seconds=online_threshold_seconds())


def _as_aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def is_recent(last_activity: Optional[datetime]) -> bool:
    """True when last_activity is within the online threshold. Accepts naive (UTC) or aware values."""
    if last_activity is None:
        return False
    return _as_aware_utc(last_activity) >= online_cutoff()


def is_expired(expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True when a session's expires_at is in the past. Accepts naive (UTC) or aware values."""
    if expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return _as_aware_utc(expires_at) <= now


def should_touch(last_activity: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True when the session's last_activity is older than the touch interval."""
    if last_activity is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - _as_aware_utc(last_activity)) >= timedelta(seconds=activity_touch_interval_seconds())


def online_cutoff_param():
    """Bind parameter evaluated at statement execution, usable inside column properties and filters."""
    return bindparam("presence_online_cutoff", callable_=online_cutoff)


def online_user_ids_select():
    """SELECT user_id FROM user_sessions WHERE active AND touched within the threshold."""
    from app.backend.models.user import UserSession
    return sa_select(UserSession.user_id).where(
        UserSession.is_active == True,
        UserSession.last_activity >= online_cutoff_param(),
    )
