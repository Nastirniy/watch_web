"""Which sessions of the streams are still alive: one batched query every events.session_check_seconds.

One definition with REST: live_clause() of auth/sessions.py (not ended, before its end, used within the idle timeout,
inside the hard limit), the account active and not archived, and a session bound to a certificate only while that
certificate is an active row of the user (state_clause of services/certificates.py). This is what ends the stream of a
revoked certificate: nginx judges a connection once, at its handshake, and an open stream kept receiving after the
revoke, the CRL and a reload (measured). No publish at each place that ends a session: an idle end is computed,
nobody writes it.
"""
from typing import Sequence, Set

from sqlalchemy import and_, exists, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.sessions import live_clause
from app.backend.enums.general import CertificateState
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.certificates import state_clause


async def live_sessions(session: AsyncSession, session_ids: Sequence[str], chunk: int = 500) -> Set[str]:
    """The ids among session_ids whose session is alive now"""
    ids = sorted(set(session_ids))
    alive: Set[str] = set()
    for start in range(0, len(ids), chunk):
        query = (
            select(UserSession.id)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.id.in_(ids[start:start + chunk]),
                live_clause(),
                User.is_active.is_(True),
                User.is_deleted.is_(False),
                or_(UserSession.certificate_fingerprint_hash.is_(None),
                    exists().where(and_(UserCertificate.fingerprint_hash == UserSession.certificate_fingerprint_hash,
                                        UserCertificate.user_id == UserSession.user_id,
                                        state_clause(CertificateState.ACTIVE)))),
            )
        )
        alive.update((await session.exec(query)).all())
    return alive
