"""The batched session check of the streams: one test per way a session ends, and a live one stays"""
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.config import settings
from app.backend.models.user import UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.events.session_check import live_sessions
from tests.helpers import make_user


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _session(db, user, **values) -> UserSession:
    now = _now()
    row = UserSession(id=secrets.token_urlsafe(16), user_id=user.id, created_at=now, last_activity=now,
                      expires_at=now + timedelta(minutes=30))
    for key, value in values.items():
        setattr(row, key, value)
    db.add(row)
    await db.commit()
    return row


async def _certificate(db, user, **values) -> UserCertificate:
    row = UserCertificate(user_id=user.id, source="uploaded", fingerprint_hash=secrets.token_hex(32),
                          cn_hash=secrets.token_hex(32), cn_hash_scheme=2, serial_hash=secrets.token_hex(32), **values)
    db.add(row)
    await db.commit()
    return row


async def _alive(db, row) -> bool:
    return row.id in await live_sessions(db, [row.id])


async def test_a_live_session_stays(db):
    user = await make_user(db)
    certificate = await _certificate(db, user, not_after=_now() + timedelta(days=30))
    plain, bound = await _session(db, user), await _session(db, user, certificate_fingerprint_hash=certificate.fingerprint_hash)
    assert await live_sessions(db, [plain.id, bound.id, "unknown"]) == {plain.id, bound.id}
    assert await live_sessions(db, []) == set()


async def test_ended(db):
    assert not await _alive(db, await _session(db, await make_user(db), ended_at=_now(), end_reason="logout"))


async def test_past_its_end(db):
    assert not await _alive(db, await _session(db, await make_user(db), expires_at=_now() - timedelta(seconds=1)))


async def test_idle_past_the_timeout(db):
    idle = _now() - timedelta(minutes=settings.security.session_idle_timeout_minutes + 1)
    assert not await _alive(db, await _session(db, await make_user(db), last_activity=idle))


async def test_past_the_hard_limit(db, monkeypatch):
    monkeypatch.setattr(settings.security, "session_max_hours", 1)
    row = await _session(db, await make_user(db), created_at=_now() - timedelta(hours=2))
    assert not await _alive(db, row)


@pytest.mark.parametrize("flags", [{"is_active": False}, {"is_deleted": True}])
async def test_the_account_deactivated_or_archived(db, flags):
    user = await make_user(db)
    row = await _session(db, user)
    for key, value in flags.items():
        setattr(user, key, value)
    db.add(user)
    await db.commit()
    assert not await _alive(db, row)


async def test_the_certificate_revoked(db):
    user = await make_user(db)
    certificate = await _certificate(db, user, revoked_at=_now())
    assert not await _alive(db, await _session(db, user, certificate_fingerprint_hash=certificate.fingerprint_hash))


async def test_the_certificate_expired(db):
    user = await make_user(db)
    certificate = await _certificate(db, user, not_after=_now() - timedelta(minutes=1))
    assert not await _alive(db, await _session(db, user, certificate_fingerprint_hash=certificate.fingerprint_hash))


async def test_a_certificate_of_another_user(db):
    owner, user = await make_user(db), await make_user(db)
    certificate = await _certificate(db, owner)
    assert not await _alive(db, await _session(db, user, certificate_fingerprint_hash=certificate.fingerprint_hash))
