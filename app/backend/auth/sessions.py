"""Login sessions: state computed when read, lifetimes from settings.security.

A session lives while it is used: every request and refresh moves its end (expires_at) to
last activity + security.session_idle_timeout_minutes (touch). It ends at the first of: logout or revocation
(ended_at stored), that end passing without a request, or, when security.session_max_hours is set, that long
after the login whatever the activity. The state is always computed from those values (session_state,
live_clause), so nothing needs a cleanup job to be correct: record_end only writes down an end the computation
already found.

A session opened with a client certificate (production login through nginx) is bound to it (RFC 8705 style):
every request must present the same certificate (certificate_matches), so a token is useless without it, and
that certificate must still be an active certificate of the user
(AuthService.end_session_of_revoked_certificate).

A refresh token works once: the session stores the jti of the current one (refresh_jti) and every refresh
replaces it (claim_refresh), so a copy of an older token refreshes nothing.
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import bindparam, or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.auth.certificates import hash_data
from app.backend.models.user import User, UserSession
from app.backend.auth.presence import is_recent
from app.backend.utils.logging_config import get_auth_logger
from app.backend.utils.row_locks import lock_first

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


async def begin_session_write(session: AsyncSession, user_id: int) -> None:
    """The first statement of a request's write to sessions of a user (touch, refresh, a recorded end): the user row
    locked shared (utils/row_locks.py lock_first). Every write that ends sessions of the user (an administrator
    ending them, a deactivation, an archive, a certificate revocation) locks that row FOR UPDATE first: a request
    waits for it and then finds its session ended, so that write never meets a session row changed after its reads
    (MariaDB error 1020), and the user row always comes before the session rows (no deadlock, 1213)."""
    await lock_first(session, select(User.id).where(User.id == user_id), shared=True)


async def touch_session(session: AsyncSession, row: UserSession, now: Optional[datetime] = None) -> bool:
    """touch() for an authenticated request, as a write transaction of its own: it ends the read transaction the
    request opened, takes a shared lock on the user row (begin_session_write: an administrator ending the user's
    sessions or revoking a certificate holds it FOR UPDATE) and runs one conditional UPDATE, which moves
    last_activity only forward from the value this request read. Parallel requests of one session (a page loading
    several lists) wait for each other on the row instead of updating a row changed after their snapshot (MariaDB
    error 1020); the later ones change nothing. A touch during such an administrator's write waits
    for it and then finds the session ended, so that write never meets a touch committed after its reads (1020).
    Committed here; False when another request touched or ended the session first."""
    now = now or _now()
    expires_at = deadline(row, now)
    await begin_session_write(session, row.user_id)
    result = await session.exec(
        update(UserSession)
        .where(UserSession.id == row.id, UserSession.last_activity <= row.last_activity,
               UserSession.ended_at.is_(None))
        .values(last_activity=now, expires_at=expires_at)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount == 0:
        return False
    row.last_activity, row.expires_at = now, expires_at     # the request's own copy shows the touch
    return True


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


async def record_end(session: AsyncSession, row: UserSession, state: SessionState, begin: bool = True) -> None:
    """Write down an end the computation found (expired / idle timeout) at the moment it happened.

    One conditional UPDATE (ended_at still empty): parallel requests of an expired session find the same end, the
    first one writes it and the others change nothing, and an end recorded in between (a revocation) is kept.
    begin=True (a request that only read so far) runs it as a write of its own, after the read transaction (MariaDB
    error 1020 otherwise), beginning with the shared user lock (begin_session_write); a caller whose write already
    began (the user locked, the refresh claim) passes begin=False. Committed here."""
    if row.ended_at is not None or state.status is not SessionStatus.ENDED:
        return
    ended_at, reason = state.ends_at or _now(), state.end_reason.value if state.end_reason else None
    if begin:
        await begin_session_write(session, row.user_id)
    result = await session.exec(
        update(UserSession)
        .where(UserSession.id == row.id, UserSession.ended_at.is_(None))
        .values(ended_at=ended_at, end_reason=reason, revoke_cause=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount:
        row.ended_at, row.end_reason, row.revoke_cause = ended_at, reason, None


def new_refresh_jti() -> str:
    """The id of a refresh token (its jti claim), stored on the session as the one that may refresh it"""
    return secrets.token_urlsafe(32)


async def claim_refresh(session: AsyncSession, session_id: str, user_id: int, presented_jti: str) -> Optional[str]:
    """Retire the refresh token presented and record the id of the next one: that id, or None when the presented
    token is not the session's current one (used already, or the session is not the user's).

    One conditional UPDATE, the first change of the refresh transaction (after the shared user lock the caller
    takes first, begin_session_write; a locking read opens no snapshot): of two refreshes with the same token
    exactly one wins, and no row read earlier in the transaction can change under it (MariaDB's snapshot isolation
    answers a write to such a row with error 1020). A session opened before migration 025 has no
    refresh_jti and takes any of its tokens once. The caller's transaction decides: a refresh refused after the
    claim rolls it back, and the token stays usable.
    """
    next_jti = new_refresh_jti()
    result = await session.execute(
        update(UserSession)
        .execution_options(synchronize_session=False)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            or_(UserSession.refresh_jti == presented_jti, UserSession.refresh_jti.is_(None)),
        )
        .values(refresh_jti=next_jti)
    )
    return next_jti if result.rowcount == 1 else None


async def live_session(session: AsyncSession, session_id: str, user_id: int, begin: bool = True) -> Optional[UserSession]:
    """The user's session when it is live, else None (an end found by the computation is recorded first; begin as
    for record_end: False when the caller's write already began, the refresh claim)"""
    row = (await session.exec(
        select(UserSession).where(UserSession.id == session_id, UserSession.user_id == user_id)
    )).first()
    if row is None:
        _logger.warning(f"Session not found: session_id={session_id}, user_id={user_id}")
        return None
    state = session_state(row)
    if state.status is SessionStatus.ENDED:
        await record_end(session, row, state, begin=begin)
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
