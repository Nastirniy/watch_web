"""Login sessions: state computed when read, lifetimes from settings.security.

A session is live until the first of: logout or revocation (ended_at stored), expires_at (absolute,
security.session_max_hours after login), last_activity + security.session_idle_timeout_minutes. The state is
always computed from those values (session_state, live_clause), so nothing needs a cleanup job to be correct:
marking a past session ended (record_end) only writes down what the computation already says.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import bindparam
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.models.user import UserSession
from app.backend.services.presence import is_recent
from app.backend.utils.logging_config import get_auth_logger

_logger = get_auth_logger()


def utc(value: Optional[datetime]) -> Optional[datetime]:
    """Aware UTC; the database returns naive UTC values"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def idle_timeout() -> timedelta:
    return timedelta(minutes=settings.security.session_idle_timeout_minutes)


def session_lifetime() -> timedelta:
    return timedelta(hours=settings.security.session_max_hours)


@dataclass(frozen=True)
class SessionState:
    status: SessionStatus
    end_reason: Optional[SessionEndReason] = None
    revoke_cause: Optional[SessionRevokeCause] = None
    ended_at: Optional[datetime] = None          # stored, or the moment the computation says it ended
    idle_expires_at: Optional[datetime] = None   # live only: when it ends if no request comes


def session_state(row: UserSession, now: Optional[datetime] = None) -> SessionState:
    now = now or datetime.now(timezone.utc)
    if row.ended_at is not None:
        return SessionState(
            status=SessionStatus.ENDED,
            end_reason=SessionEndReason(row.end_reason) if row.end_reason else None,
            revoke_cause=SessionRevokeCause(row.revoke_cause) if row.revoke_cause else None,
            ended_at=utc(row.ended_at),
        )
    expires_at = utc(row.expires_at)
    idle_deadline = utc(row.last_activity) + idle_timeout()
    if now >= min(expires_at, idle_deadline):
        if expires_at <= idle_deadline:
            return SessionState(status=SessionStatus.ENDED, end_reason=SessionEndReason.EXPIRED, ended_at=expires_at)
        return SessionState(status=SessionStatus.ENDED, end_reason=SessionEndReason.IDLE_TIMEOUT, ended_at=idle_deadline)
    status = SessionStatus.ONLINE if is_recent(row.last_activity) else SessionStatus.IDLE
    return SessionState(status=status, idle_expires_at=min(idle_deadline, expires_at))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _idle_cutoff() -> datetime:
    return _now() - idle_timeout()


def live_clause():
    """SQL condition of a live session, evaluated at statement execution (usable in column properties)"""
    return (
        UserSession.ended_at.is_(None)
        & (UserSession.expires_at > bindparam("session_now", callable_=_now))
        & (UserSession.last_activity > bindparam("session_idle_cutoff", callable_=_idle_cutoff))
    )


def end(row: UserSession, reason: SessionEndReason, cause: Optional[SessionRevokeCause] = None,
        at: Optional[datetime] = None) -> None:
    """Mark the session ended (the caller commits)"""
    row.ended_at = at or _now()
    row.end_reason = reason.value
    row.revoke_cause = cause.value if cause else None


async def record_end(session: AsyncSession, row: UserSession, state: SessionState) -> None:
    """Write down an end the computation found (expired / idle timeout) at the moment it happened"""
    if row.ended_at is None and state.status is SessionStatus.ENDED:
        end(row, state.end_reason, at=state.ended_at)
        session.add(row)
        await session.commit()


async def live_session(session: AsyncSession, session_id: str, user_id: int) -> Optional[UserSession]:
    """The user's session when it is live, else None (an end found by the computation is recorded first)"""
    row = (await session.exec(
        select(UserSession).where(UserSession.id == session_id, UserSession.user_id == user_id)
    )).first()
    if row is None:
        _logger.warning(f"Session not found: session_id={session_id}, user_id={user_id}")
        return None
    state = session_state(row)
    if state.status is SessionStatus.ENDED:
        await record_end(session, row, state)
        _logger.warning(f"Session ended ({state.end_reason.value if state.end_reason else 'unknown'}): "
                        f"session_id={session_id}, user_id={user_id}")
        return None
    return row
