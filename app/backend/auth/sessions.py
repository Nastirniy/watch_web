"""Login sessions: state computed when read, lifetimes from settings.security.

A session lives while it is used: every request and refresh moves its end (expires_at) to
last activity + security.session_idle_timeout_minutes (touch). It ends at the first of: logout or revocation
(ended_at stored), that end passing without a request, or, when security.session_max_hours is set, that long
after the login whatever the activity. The state is always computed from those values (session_state,
live_clause), so nothing needs a cleanup job to be correct: record_end only writes down an end the computation
already found.

A session opened with a client certificate (production login through nginx) is bound to it (RFC 8705 style):
every request must present the same certificate (certificate_matches), so a token is useless without it.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import bindparam
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.auth.certificates import hash_data
from app.backend.models.user import UserSession
from app.backend.auth.presence import is_recent
from app.backend.utils.logging_config import get_auth_logger

_logger = get_auth_logger()
_NO_CAP = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc(value: Optional[datetime]) -> Optional[datetime]:
    """Aware UTC; the database returns naive UTC values"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def idle_timeout() -> timedelta:
    return timedelta(minutes=settings.security.session_idle_timeout_minutes)


def session_cap() -> Optional[timedelta]:
    """The absolute limit since the login, or None (security.session_max_hours = 0: activity keeps it alive)"""
    hours = settings.security.session_max_hours
    return timedelta(hours=hours) if hours else None


def hard_end(row: UserSession) -> Optional[datetime]:
    """login + security.session_max_hours, or None without a cap"""
    cap = session_cap()
    return utc(row.created_at) + cap if cap is not None else None


def deadline(row: UserSession, activity: datetime) -> datetime:
    """When the session ends unless a request comes after activity: the idle timeout, never past the hard end"""
    end_at = utc(activity) + idle_timeout()
    hard = hard_end(row)
    return min(end_at, hard) if hard is not None else end_at


def touch(row: UserSession, now: Optional[datetime] = None) -> None:
    """A request or a refresh: the activity is now and the end moves with it (the caller commits)"""
    now = now or _now()
    row.last_activity = now
    row.expires_at = deadline(row, now)


@dataclass(frozen=True)
class SessionState:
    status: SessionStatus
    ends_at: datetime                            # live: when it ends unless a request comes; ended: when it ended
    end_reason: Optional[SessionEndReason] = None
    revoke_cause: Optional[SessionRevokeCause] = None


def session_state(row: UserSession, now: Optional[datetime] = None) -> SessionState:
    now = now or _now()
    if row.ended_at is not None:
        return SessionState(
            status=SessionStatus.ENDED,
            ends_at=utc(row.ended_at),
            end_reason=SessionEndReason(row.end_reason) if row.end_reason else None,
            revoke_cause=SessionRevokeCause(row.revoke_cause) if row.revoke_cause else None,
        )
    inactivity_end = utc(row.last_activity) + idle_timeout()
    ends_at = min(utc(row.expires_at), deadline(row, row.last_activity))
    if now >= ends_at:
        # Ended before its inactivity deadline: the hard end (or a stored end from before sliding sessions)
        reason = SessionEndReason.EXPIRED if ends_at < inactivity_end else SessionEndReason.IDLE_TIMEOUT
        return SessionState(status=SessionStatus.ENDED, ends_at=ends_at, end_reason=reason)
    status = SessionStatus.ONLINE if is_recent(row.last_activity) else SessionStatus.IDLE
    return SessionState(status=status, ends_at=ends_at)


def _idle_cutoff() -> datetime:
    return _now() - idle_timeout()


def _hard_cutoff() -> datetime:
    cap = session_cap()
    return _now() - cap if cap is not None else _NO_CAP


def live_clause():
    """SQL condition of a live session, evaluated at statement execution (usable in column properties)"""
    return (
        UserSession.ended_at.is_(None)
        & (UserSession.expires_at > bindparam("session_now", callable_=_now))
        & (UserSession.last_activity > bindparam("session_idle_cutoff", callable_=_idle_cutoff))
        & (UserSession.created_at > bindparam("session_hard_cutoff", callable_=_hard_cutoff))
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
        end(row, state.end_reason, at=state.ends_at)
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


async def bind_certificate(row: UserSession, fingerprint: Optional[str]) -> None:
    """Bind a new session to the client certificate it was opened with (nginx X-SSL-Client-Fingerprint)"""
    row.certificate_fingerprint_hash = await hash_data(fingerprint) if fingerprint else None


async def certificate_matches(row: UserSession, fingerprint: Optional[str]) -> bool:
    """A bound session accepts only its own certificate; an unbound one (development login) any request"""
    if not row.certificate_fingerprint_hash:
        return True
    return bool(fingerprint) and await hash_data(fingerprint) == row.certificate_fingerprint_hash
