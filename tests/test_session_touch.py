"""Session touch on authenticated requests: a write transaction of its own, one conditional UPDATE.

Live on MariaDB (snapshot isolation) parallel requests of one session after the touch interval all updated the row
they had read: the losers answered 500 (error 1020). The touch now ends the read transaction first and only moves
last_activity forward from the value the request read; a request that lost the race changes nothing."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.backend.auth.sessions import TouchResult, touch_session, utc
from app.backend.models.user import UserSession
from tests.helpers import make_user


async def _row(db, user, last_activity):
    row = UserSession(id=f"touch-{user.id}", user_id=user.id, created_at=last_activity, last_activity=last_activity,
                      expires_at=last_activity + timedelta(minutes=30))
    db.add(row)
    await db.commit()
    return row


async def _stored(session_maker, session_id):
    async with session_maker() as fresh:
        return (await fresh.exec(select(UserSession).where(UserSession.id == session_id))).one()


async def test_touch_moves_activity_and_end(db, session_maker):
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = await _row(db, user, then)
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(session, read) is TouchResult.TOUCHED
    stored = await _stored(session_maker, row.id)
    assert utc(stored.last_activity) > then + timedelta(minutes=4)
    assert utc(stored.expires_at) > utc(stored.last_activity)


async def test_a_request_that_read_an_older_activity_changes_nothing(db, session_maker):
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = await _row(db, user, then)
    async with session_maker() as slow, session_maker() as fast:
        stale = (await slow.exec(select(UserSession).where(UserSession.id == row.id))).one()
        current = (await fast.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(fast, current) is TouchResult.TOUCHED
        moved = (await _stored(session_maker, row.id)).last_activity
        assert await touch_session(slow, stale) is TouchResult.BEHIND
    assert (await _stored(session_maker, row.id)).last_activity == moved


async def test_an_end_recorded_by_another_request_is_kept(db, session_maker):
    """Parallel requests of an expired session each recorded its end: the later ones wrote over the row the first
    one changed (a 500 on MariaDB, 1020) and could overwrite an end recorded in between (a revocation)"""
    from app.backend.auth.sessions import record_end, session_state
    from app.backend.enums.general import SessionEndReason, SessionRevokeCause
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(days=2)
    row = await _row(db, user, then)
    async with session_maker() as stale_session:
        stale = (await stale_session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        async with session_maker() as admin:
            ended = (await admin.exec(select(UserSession).where(UserSession.id == row.id))).one()
            ended.ended_at, ended.end_reason = datetime.now(timezone.utc), SessionEndReason.REVOKED.value
            ended.revoke_cause = SessionRevokeCause.ENDED_BY_ADMIN.value
            admin.add(ended)
            await admin.commit()
        await record_end(stale_session, stale, session_state(stale))
    stored = await _stored(session_maker, row.id)
    assert stored.end_reason == SessionEndReason.REVOKED.value


async def test_touch_never_moves_an_ended_session_and_updates_the_row_in_memory(db, session_maker):
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = await _row(db, user, then)
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(session, read) is TouchResult.TOUCHED
        assert utc(read.last_activity) > then + timedelta(minutes=4)     # the caller's copy shows the touch
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        async with session_maker() as other:
            ended = (await other.exec(select(UserSession).where(UserSession.id == row.id))).one()
            ended.ended_at, ended.end_reason = datetime.now(timezone.utc), "logout"
            other.add(ended)
            await other.commit()
        assert await touch_session(session, read) is TouchResult.ENDED     # read before the logout: never moves an ended row


async def test_ending_a_session_of_an_inactive_certificate_twice_keeps_the_first_end(db, session_maker):
    """Every parallel request of a session whose certificate expired ended the session: the later ones wrote over
    the row the first one changed (1020 on MariaDB) and logged a second force_logout"""
    from app.backend.auth.service import AuthService
    from app.backend.models.audit import AuditLog
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=1))
    row.certificate_fingerprint_hash = "no-such-certificate"
    db.add(row)
    await db.commit()
    async with session_maker() as first, session_maker() as second:
        a = (await first.exec(select(UserSession).where(UserSession.id == row.id))).one()
        b = (await second.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await AuthService.end_session_of_revoked_certificate(first, a, user) is True
        assert await AuthService.end_session_of_revoked_certificate(second, b, user) is True
    stored = await _stored(session_maker, row.id)
    assert stored.end_reason == "revoked" and stored.revoke_cause == "certificate_revoked"
    async with session_maker() as fresh:
        logged = (await fresh.exec(select(AuditLog).where(AuditLog.action == "end",
                                                          AuditLog.session_id == row.id))).all()
    assert len(logged) == 1


def _statements(engine):
    """The statements executed while the returned list is open, whitespace folded, with "COMMIT" where a
    transaction ended (sqlite drops FOR UPDATE / FOR SHARE: the order is what the tests can see)"""
    seen = []

    def execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    def commit(conn):
        seen.append("COMMIT")

    event.listen(engine.sync_engine, "before_cursor_execute", execute)
    event.listen(engine.sync_engine, "commit", commit)

    def stop():
        event.remove(engine.sync_engine, "before_cursor_execute", execute)
        event.remove(engine.sync_engine, "commit", commit)
    return seen, stop


def _write_of(seen, marker):
    """The statements of the transaction that ran the first statement starting with marker"""
    at = next(i for i, statement in enumerate(seen) if statement.startswith(marker))
    begin = max((i for i in range(at) if seen[i] == "COMMIT"), default=-1) + 1
    end = next((i for i in range(at, len(seen)) if seen[i] == "COMMIT"), len(seen))
    return seen[begin:end]


async def test_touch_takes_a_shared_lock_on_its_user_first(db, session_maker, engine):
    """Live on MariaDB an administrator ending the sessions of a user (or revoking a certificate) read the sessions
    and then updated them; a touch committed in between made that update answer 500 (error 1020). Every writer of a
    user's sessions now takes the user row first: the administrator FOR UPDATE, the touch shared."""
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=5))
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        seen, stop = _statements(engine)
        try:
            assert await touch_session(session, read) is TouchResult.TOUCHED
        finally:
            stop()
    write = _write_of(seen, "UPDATE user_sessions")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("UPDATE user_sessions"), write


async def test_certificate_revoke_locks_the_user_first(client, db, headers, engine):
    """The revocation ends the sessions of the certificate: like every other end of a user's sessions it begins with
    the user row, so it waits for a touch of the user instead of meeting it (1020)"""
    from app.backend.api.v1 import V1
    from app.backend.enums.general import CertificateSource
    from app.backend.enums.routes import UserRoutes
    from tests.helpers import make_admin, make_certificate_row
    admin = await make_admin(db)
    user = await make_user(db)
    certificate = await make_certificate_row(db, user, CertificateSource.LEGACY)
    seen, stop = _statements(engine)
    try:
        response = await client.post(
            V1.path(UserRoutes.USER_CERTIFICATE_REVOKE, user_id=user.id, certificate_id=certificate.id),
            headers=headers(admin))
    finally:
        stop()
    assert response.status_code == 200, response.text
    write = _write_of(seen, "UPDATE user_certificates")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    # Then the user entity (lock_certificate_owner), the checks of the target, the certificate row
    assert write[1].startswith("SELECT users.id, users.username"), write
    assert any(statement.startswith("SELECT user_certificates.") for statement in write[2:]), write


def test_the_touch_lock_is_shared(monkeypatch):
    """A shared lock (LOCK IN SHARE MODE on MariaDB), never FOR UPDATE: touches of one user's sessions run side by
    side and wait only for a write that holds the user row FOR UPDATE"""
    import asyncio
    from sqlalchemy.dialects import mysql
    from app.backend.auth import sessions
    from app.backend.utils.row_locks import locking
    taken = []

    async def record(session, query, shared=False):
        taken.append(locking(query, shared))
        raise LookupError("recorded")             # stop before the UPDATE: no database needed

    monkeypatch.setattr(sessions, "lock_first", record)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = UserSession(id="shared", user_id=7, created_at=then, last_activity=then, expires_at=then + timedelta(hours=1))
    try:
        asyncio.run(touch_session(None, row))
    except LookupError:
        pass
    (statement,) = taken
    sql = str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FROM users" in sql and "LOCK IN SHARE MODE" in sql and "FOR UPDATE" not in sql, sql


async def test_refresh_takes_a_shared_lock_on_its_user_first(client, db, engine):
    """A refresh is activity like a touch: it shares the user lock before it claims its token, so it waits for an
    administrator ending the user's sessions instead of changing a session row that write reads and then ends"""
    from app.backend.api.v1 import V1
    from app.backend.auth.service import AuthService
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh = AuthService.session_tokens(user.id, row)[1]
    seen, stop = _statements(engine)
    try:
        response = await client.post(V1.path(AuthRoutes.REFRESH), json={"refresh_token": refresh})
    finally:
        stop()
    assert response.status_code == 200, response.text
    write = _write_of(seen, "UPDATE user_sessions SET refresh_jti")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("UPDATE user_sessions SET refresh_jti"), write


async def test_a_refresh_after_an_admin_end_is_session_expired(client, db, headers):
    """What a refresh that waited behind the administrator's end finds: the session ended"""
    from app.backend.api.v1 import V1
    from app.backend.auth.service import AuthService
    from app.backend.enums.routes import AuthRoutes, UserRoutes
    from tests.helpers import make_admin
    admin = await make_admin(db)
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh = AuthService.session_tokens(user.id, row)[1]
    ended = await client.post(V1.path(UserRoutes.USER_SESSIONS_END, user_id=user.id), headers=headers(admin))
    assert ended.status_code == 200 and ended.json()["ended_count"] == 1, ended.text
    response = await client.post(V1.path(AuthRoutes.REFRESH), json={"refresh_token": refresh})
    assert response.status_code == 401 and response.json()["code"] == "session_expired", response.text


async def test_ending_a_session_of_an_inactive_certificate_shares_the_user_lock_first(db, session_maker, engine):
    """It ended the session row and then wrote the audit row of the user (a shared lock on the user row through the
    foreign key): the reverse of an administrator's order, a deadlock (1213) against an end of the user's sessions"""
    from app.backend.auth.service import AuthService
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=1))
    row.certificate_fingerprint_hash = "no-such-certificate"
    db.add(row)
    await db.commit()
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        seen, stop = _statements(engine)
        try:
            assert await AuthService.end_session_of_revoked_certificate(session, read, user) is True
        finally:
            stop()
    write = _write_of(seen, "UPDATE user_sessions")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("UPDATE user_sessions"), write


async def test_recording_an_end_shares_the_user_lock_first(db, session_maker, engine):
    from app.backend.auth.sessions import record_end, session_state
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(days=2))
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        seen, stop = _statements(engine)
        try:
            await record_end(session, read, session_state(read))
        finally:
            stop()
    assert read.ended_at is not None
    write = _write_of(seen, "UPDATE user_sessions")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("UPDATE user_sessions"), write


# ------------------------------------------------------------------------------------------------------------------
# A request's writes to its session against an administrator's end of it (MariaDB: the request's shared user lock
# waits for the administrator's FOR UPDATE, then runs). sqlite cannot race: the administrator's commit is placed
# where MariaDB would let it land, inside the request's begin_session_write, before its lock.

def _with_rollbacks(engine):
    """_statements plus "ROLLBACK" where a transaction was rolled back"""
    seen, stop = _statements(engine)

    def rollback(conn):
        seen.append("ROLLBACK")

    event.listen(engine.sync_engine, "rollback", rollback)

    def stop_all():
        event.remove(engine.sync_engine, "rollback", rollback)
        stop()
    return seen, stop_all


def _transaction_of(seen, marker):
    """The statements of the transaction that ran the first statement starting with marker (COMMIT or ROLLBACK
    ends one)"""
    at = next(i for i, statement in enumerate(seen) if statement.startswith(marker))
    begin = max((i for i in range(at) if seen[i] in ("COMMIT", "ROLLBACK")), default=-1) + 1
    end = next((i for i in range(at, len(seen)) if seen[i] in ("COMMIT", "ROLLBACK")), len(seen))
    return seen[begin:end + 1]


async def _logged_in(db, user, idle_for: timedelta):
    """A live session of the user (unbound, as the development login) whose last request was idle_for ago (a touch
    is due from 60 s), and the Bearer header of an access token bound to it"""
    from app.backend.auth.service import AuthService
    from app.backend.config import settings
    row = await AuthService.create_session(db, user)
    then = datetime.now(timezone.utc) - idle_for
    row.last_activity = then
    row.expires_at = then + timedelta(minutes=settings.security.session_idle_timeout_minutes)
    db.add(row)
    await db.commit()
    return row, {"Authorization": f"Bearer {AuthService.session_tokens(user.id, row)[0]}"}


def _meanwhile(monkeypatch, change):
    """change() commits in a session of its own just before the request's first session write takes its user lock
    (sessions.begin_session_write or begin_account_session_write): what MariaDB does when an administrator holds the
    user row FOR UPDATE at that moment"""
    from app.backend.auth import sessions
    pending = [change]

    def after_the_other_write(original):
        async def locked(session, user_id):
            if pending:
                await pending.pop()()
            return await original(session, user_id)
        return locked

    for name in ("begin_session_write", "begin_account_session_write"):
        if hasattr(sessions, name):
            monkeypatch.setattr(sessions, name, after_the_other_write(getattr(sessions, name)))


def _end_session(session_maker, session_id, cause, deactivate_user_id=None, archive_user_id=None):
    from app.backend.enums.general import SessionEndReason
    from app.backend.models.user import User

    async def change():
        async with session_maker() as other:
            row = (await other.exec(select(UserSession).where(UserSession.id == session_id))).one()
            row.ended_at, row.end_reason = datetime.now(timezone.utc), SessionEndReason.REVOKED.value
            row.revoke_cause = cause.value
            other.add(row)
            for user_id, field in ((deactivate_user_id, "is_active"), (archive_user_id, "is_deleted")):
                if user_id is not None:
                    user = (await other.exec(select(User).where(User.id == user_id))).one()
                    setattr(user, field, field == "is_deleted")
                    other.add(user)
            await other.commit()
    return change


async def test_touch_tells_an_end_from_another_touch(db, session_maker):
    """touch_session is tri-state: an end committed while it waited for the user lock is ENDED (the caller's copy
    shows the end), a touch of another request first is BEHIND (the session is live)"""
    from app.backend.auth.sessions import TouchResult
    from app.backend.enums.general import SessionRevokeCause
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=5))
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        await _end_session(session_maker, row.id, SessionRevokeCause.CERTIFICATE_REVOKED)()
        assert await touch_session(session, read) is TouchResult.ENDED
        assert read.ended_at is not None and read.revoke_cause == "certificate_revoked"


async def test_touch_after_another_touch_is_behind(db, session_maker):
    from app.backend.auth.sessions import TouchResult
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=5))
    async with session_maker() as slow, session_maker() as fast:
        stale = (await slow.exec(select(UserSession).where(UserSession.id == row.id))).one()
        current = (await fast.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(fast, current) is TouchResult.TOUCHED
        assert await touch_session(slow, stale) is TouchResult.BEHIND


@pytest.mark.parametrize("cause", ["ended_by_admin", "certificate_revoked"])
async def test_a_request_whose_touch_waited_for_an_end_of_its_session_is_refused(
        client, db, session_maker, monkeypatch, cause):
    """The touch waited behind an administrator who ended the session (or revoked its certificate) and matched no
    row: the request went on as authenticated. It answers what the next request of the session answers: an ended
    session is session_expired whatever ended it (token_certificate_mismatch is for a certificate that stopped being
    active without its sessions being ended, API.md)."""
    code = "session_expired"
    from app.backend.api.v1 import V1
    from app.backend.enums.general import SessionRevokeCause
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    row, auth_header = await _logged_in(db, user, timedelta(minutes=5))
    _meanwhile(monkeypatch, _end_session(session_maker, row.id, SessionRevokeCause(cause)))
    response = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    assert response.status_code == 401 and response.json()["code"] == code, response.text


@pytest.mark.parametrize("archived, code", [(False, "account_deactivated"), (True, "account_deleted")])
async def test_a_request_whose_touch_waited_for_a_deactivation_is_refused(
        client, db, session_maker, monkeypatch, archived, code):
    from app.backend.api.v1 import V1
    from app.backend.enums.general import SessionRevokeCause
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    row, auth_header = await _logged_in(db, user, timedelta(minutes=5))
    cause = SessionRevokeCause.ACCOUNT_ARCHIVED if archived else SessionRevokeCause.ACCOUNT_DEACTIVATED
    _meanwhile(monkeypatch, _end_session(session_maker, row.id, cause,
                                         archive_user_id=user.id if archived else None,
                                         deactivate_user_id=None if archived else user.id))
    response = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    assert response.status_code == 401 and response.json()["code"] == code, response.text


async def test_a_write_whose_touch_waited_for_an_end_of_its_session_is_not_done(client, db, session_maker,
                                                                                  monkeypatch):
    from sqlalchemy import func
    from app.backend.api.v1 import V1
    from app.backend.enums.general import SessionRevokeCause
    from app.backend.enums.routes import ObservationRoutes
    from app.backend.models.observation import Observation
    from tests.helpers import make_admin, make_dictionary, make_post, observation_payload
    admin = await make_admin(db)
    post = await make_post(db)
    dictionary = await make_dictionary(db)
    row, auth_header = await _logged_in(db, admin, timedelta(minutes=5))
    _meanwhile(monkeypatch, _end_session(session_maker, row.id, SessionRevokeCause.ENDED_BY_ADMIN))
    response = await client.post(V1.path(ObservationRoutes.CREATE), headers=auth_header,
                                 json=observation_payload(post.id, dictionary["signal_type_id"]))
    async with session_maker() as fresh:
        stored = (await fresh.exec(select(func.count()).select_from(Observation))).one()
    assert response.status_code == 401 and response.json()["code"] == "session_expired", response.text
    assert stored == 0


async def test_a_request_whose_touch_lost_to_another_touch_goes_on(client, db, session_maker, monkeypatch):
    from app.backend.api.v1 import V1
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    row, auth_header = await _logged_in(db, user, timedelta(minutes=5))

    async def another_request_touched():
        async with session_maker() as other:
            moved = (await other.exec(select(UserSession).where(UserSession.id == row.id))).one()
            moved.last_activity = datetime.now(timezone.utc)
            other.add(moved)
            await other.commit()

    _meanwhile(monkeypatch, another_request_touched)
    response = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------------------------------------------- logout

async def test_logout_is_one_conditional_update_after_the_user_lock(client, db, engine, session_maker):
    """Logout read the session and updated it in the transaction its authentication opened, with no user lock first:
    on MariaDB 1020 (a 500) whenever an administrator's end, a refresh or a touch of the session committed between
    its first read and its update"""
    from app.backend.api.v1 import V1
    from app.backend.enums.routes import AuthRoutes
    from app.backend.models.audit import AuditLog
    user = await make_user(db)
    row, auth_header = await _logged_in(db, user, timedelta(seconds=5))            # no touch: the common case
    seen, stop = _with_rollbacks(engine)
    try:
        response = await client.post(V1.path(AuthRoutes.LOGOUT), headers=auth_header)
    finally:
        stop()
    assert response.status_code == 200, response.text
    write = _transaction_of(seen, "UPDATE user_sessions")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("UPDATE user_sessions SET ended_at"), write
    assert [s for s in write[2:] if not s.startswith("INSERT INTO audit_logs")] == ["COMMIT"], write
    stored = await _stored(session_maker, row.id)
    assert stored.end_reason == "logout" and stored.ended_at is not None and stored.revoke_cause is None
    async with session_maker() as fresh:
        logged = (await fresh.exec(select(AuditLog).where(AuditLog.action == "logout",
                                                          AuditLog.session_id == row.id))).all()
    assert len(logged) == 1


async def test_logout_of_a_session_ended_meanwhile_is_already_invalidated(client, db, session_maker, monkeypatch):
    from app.backend.api.v1 import V1
    from app.backend.enums.general import SessionRevokeCause
    from app.backend.enums.routes import AuthRoutes
    from app.backend.models.audit import AuditLog
    user = await make_user(db)
    row, auth_header = await _logged_in(db, user, timedelta(seconds=5))
    _meanwhile(monkeypatch, _end_session(session_maker, row.id, SessionRevokeCause.ENDED_BY_ADMIN))
    response = await client.post(V1.path(AuthRoutes.LOGOUT), headers=auth_header)
    assert response.status_code == 400 and response.json()["code"] == "session_already_invalidated", response.text
    stored = await _stored(session_maker, row.id)
    assert (stored.end_reason, stored.revoke_cause) == ("revoked", "ended_by_admin")
    async with session_maker() as fresh:
        logged = (await fresh.exec(select(AuditLog).where(AuditLog.action == "logout",
                                                          AuditLog.session_id == row.id))).all()
    assert logged == []


# ---------------------------------------------------------------------------------- sessions of an inactive user

async def _deactivate(db, user, archived: bool = False):
    from app.backend.models.user import User
    stored = (await db.exec(select(User).where(User.id == user.id))).one()
    if archived:
        stored.is_deleted = True
    else:
        stored.is_active = False                 # flipped outside the API (SQL, restore of a backup)
    db.add(stored)
    await db.commit()


async def test_ending_the_sessions_of_an_inactive_user_locks_the_user_first(client, db, engine):
    """Every request of a live session of an inactive user ended the user's sessions inside the transaction its
    first read opened: of parallel requests of that client the first ended the session and every other one updated
    a row changed after its read view (MariaDB 1020: 500 instead of 401). Now the shared user lock comes first and
    the update is conditional, with no plain read before it."""
    from app.backend.api.v1 import V1
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    _, auth_header = await _logged_in(db, user, timedelta(seconds=5))
    await _deactivate(db, user)
    seen, stop = _with_rollbacks(engine)
    try:
        response = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    finally:
        stop()
    assert response.status_code == 401 and response.json()["code"] == "account_deactivated", response.text
    write = _transaction_of(seen, "UPDATE user_sessions")
    assert write[0].startswith("SELECT users.id, ") and "FROM users WHERE users.id = ?" in write[0], write
    assert "user_sessions" not in write[0], write[0]          # no consistent read inside the lock (see below)
    assert write[1].startswith("UPDATE user_sessions SET ended_at"), write


async def test_the_sessions_of_an_inactive_user_end_once(client, db, session_maker):
    """The live session is revoked (account_deactivated), one already past its idle timeout is recorded as such;
    one force_logout row, none for a later request that finds nothing to end"""
    from app.backend.api.v1 import V1
    from app.backend.enums.routes import AuthRoutes
    from app.backend.models.audit import AuditLog
    user = await make_user(db)
    live, auth_header = await _logged_in(db, user, timedelta(seconds=5))
    idle, _ = await _logged_in(db, user, timedelta(days=2))
    await _deactivate(db, user)
    first = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    again = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    assert first.status_code == again.status_code == 401
    assert first.json()["code"] == again.json()["code"] == "account_deactivated"
    stored_live, stored_idle = await _stored(session_maker, live.id), await _stored(session_maker, idle.id)
    assert (stored_live.end_reason, stored_live.revoke_cause) == ("revoked", "account_deactivated")
    assert (stored_idle.end_reason, stored_idle.revoke_cause) == ("idle_timeout", None)
    assert utc(stored_idle.ended_at) < datetime.now(timezone.utc) - timedelta(days=1)
    async with session_maker() as fresh:
        logged = (await fresh.exec(select(AuditLog).where(AuditLog.action == "end",
                                                          AuditLog.resource_id == user.id))).all()
    assert len(logged) == 1 and logged[0].params["cause"] == "account_deactivated"


async def test_a_refresh_of_an_inactive_user_ends_its_sessions_outside_its_claim(client, db, engine, session_maker):
    """The refresh ended the user's sessions after its claim of the token, in one transaction: two refreshes of two
    sessions of the user locked their own session each and then waited for the other's (1213). The claim is rolled
    back first, the end is a write of its own."""
    from app.backend.api.v1 import V1
    from app.backend.auth.service import AuthService
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh = AuthService.session_tokens(user.id, row)[1]
    await _deactivate(db, user, archived=True)
    seen, stop = _with_rollbacks(engine)
    try:
        response = await client.post(V1.path(AuthRoutes.REFRESH), json={"refresh_token": refresh})
    finally:
        stop()
    assert response.status_code == 401 and response.json()["code"] == "account_deleted", response.text
    claim = _transaction_of(seen, "UPDATE user_sessions SET refresh_jti")
    assert claim[-1] == "ROLLBACK" and not any(s.startswith("UPDATE user_sessions SET ended_at") for s in claim), claim
    write = _transaction_of(seen, "UPDATE user_sessions SET ended_at")
    assert write[0].startswith("SELECT users.id, ") and "FROM users WHERE users.id = ?" in write[0], write
    assert "user_sessions" not in write[0], write[0]          # no consistent read inside the lock (see below)
    stored = await _stored(session_maker, row.id)
    assert (stored.end_reason, stored.revoke_cause) == ("revoked", "account_archived")
    assert stored.refresh_jti == row.refresh_jti                  # the claim was rolled back


def _reactivate(session_maker, user_id):
    from app.backend.models.user import User

    async def change():
        async with session_maker() as other:
            row = (await other.exec(select(User).where(User.id == user_id))).one()
            row.is_active, row.is_deleted = True, False
            other.add(row)
            await other.commit()
    return change


async def _ended_and_logged(session_maker, user_id):
    from app.backend.models.audit import AuditLog
    async with session_maker() as fresh:
        ended = (await fresh.exec(select(UserSession).where(UserSession.user_id == user_id,
                                                            UserSession.ended_at.is_not(None)))).all()
        logged = (await fresh.exec(select(AuditLog).where(AuditLog.action == "end",
                                                          AuditLog.resource_id == user_id))).all()
    return ended, logged


@pytest.mark.parametrize("archived", [False, True])
async def test_a_request_of_an_account_reactivated_before_the_lock_ends_nothing(client, db, session_maker,
                                                                                monkeypatch, archived):
    """The request read the account inactive, an administrator reactivated it before the request's user lock: the
    flags are checked again on the locked row, nothing is ended and the request goes on as for an active user"""
    from app.backend.api.v1 import V1
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    _, auth_header = await _logged_in(db, user, timedelta(seconds=5))
    await _deactivate(db, user, archived=archived)
    _meanwhile(monkeypatch, _reactivate(session_maker, user.id))
    response = await client.get(V1.path(AuthRoutes.ME), headers=auth_header)
    assert response.status_code == 200, response.text
    assert await _ended_and_logged(session_maker, user.id) == ([], [])


async def test_a_refresh_of_an_account_reactivated_before_the_lock_ends_nothing(client, db, session_maker,
                                                                                monkeypatch):
    """Same for a refresh: its claim was rolled back before the end, so it refreshes again as for an active user"""
    from app.backend.api.v1 import V1
    from app.backend.auth.service import AuthService
    from app.backend.enums.routes import AuthRoutes
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh = AuthService.session_tokens(user.id, row)[1]
    await _deactivate(db, user)
    _meanwhile(monkeypatch, _reactivate(session_maker, user.id))
    response = await client.post(V1.path(AuthRoutes.REFRESH), json={"refresh_token": refresh})
    assert response.status_code == 200, response.text
    assert await _ended_and_logged(session_maker, user.id) == ([], [])
    again = await client.post(V1.path(AuthRoutes.REFRESH), json={"refresh_token": refresh})
    assert again.status_code == 401 and again.json()["code"] == "invalid_refresh_token", again.text


# --------------------------------------------------------------------------------- a login against a deactivation

async def _pki_user(client, db, headers, monkeypatch, username, serial):
    from app.backend.api.v1 import V1
    from app.backend.config import settings
    from app.backend.enums.general import AppEnvironment
    from app.backend.enums.routes import UserRoutes
    from tests.helpers import make_admin
    from tests.test_pki_login import make_certificate, pem
    monkeypatch.setattr(settings.app, "environment", AppEnvironment.PRODUCTION.value)
    cert = make_certificate(f"cn-{username}", serial)
    admin = await make_admin(db)
    created = await client.post(V1.path(UserRoutes.ROOT), headers=headers(admin),
                                files={"certificate": ("cert.pem", pem(cert), "application/x-pem-file")},
                                data={"username": username, "username_alias": "Race", "user_type": "viewer"})
    assert created.status_code == 200, created.text
    return created.json()["id"], cert


def _before_the_session_is_stored(monkeypatch, change):
    """change() commits between the login's check of the account (authenticate_pki, committed) and its write of the
    session: on MariaDB the deactivation holds the user row FOR UPDATE and the login's write waits for it"""
    from app.backend.auth.service import AuthService
    original = AuthService.create_session

    async def changed_first(session, user, *args, **kwargs):
        await change()
        return await original(session, user, *args, **kwargs)

    monkeypatch.setattr(AuthService, "create_session", staticmethod(changed_first))


@pytest.mark.parametrize("field, code", [("is_active", "account_deactivated"), ("is_deleted", "account_deleted"),
                                         ("revoked_at", "certificate_revoked")])
async def test_a_login_that_raced_a_deactivation_is_refused(client, db, headers, session_maker, monkeypatch,
                                                            field, code):
    """The login checked the account, committed, and stored the session in a new transaction without checking
    again: a deactivation (an archive, a revocation of the certificate) in between left a 200 with tokens and a
    live session of a deactivated account"""
    from app.backend.api.v1 import V1
    from app.backend.auth.certificates import hash_data
    from app.backend.auth.sessions import live_clause
    from app.backend.enums.routes import AuthRoutes
    from app.backend.models.user import User
    from app.backend.models.user_certificate import UserCertificate
    from tests.test_pki_login import nginx_headers
    user_id, cert = await _pki_user(client, db, headers, monkeypatch, f"race{field.replace('_', '')}", 0x4E30)

    async def change():
        async with session_maker() as other:
            if field == "revoked_at":
                fingerprint_hash = await hash_data(nginx_headers(cert)["X-SSL-Client-Fingerprint"])
                row = (await other.exec(select(UserCertificate).where(
                    UserCertificate.fingerprint_hash == fingerprint_hash))).one()
                row.revoked_at = datetime.now(timezone.utc)
            else:
                row = (await other.exec(select(User).where(User.id == user_id))).one()
                setattr(row, field, field == "is_deleted")
            other.add(row)
            await other.commit()

    _before_the_session_is_stored(monkeypatch, change)
    response = await client.post(V1.path(AuthRoutes.LOGIN), headers=nginx_headers(cert))
    async with session_maker() as fresh:
        live = (await fresh.exec(select(UserSession).where(UserSession.user_id == user_id, live_clause()))).all()
    assert response.status_code == 401 and response.json()["code"] == code, response.text
    assert live == []


async def test_a_login_stores_its_session_after_a_shared_lock_on_the_user(client, db, headers, engine, monkeypatch):
    from app.backend.api.v1 import V1
    from app.backend.enums.routes import AuthRoutes
    from tests.test_pki_login import nginx_headers
    _, cert = await _pki_user(client, db, headers, monkeypatch, "lockorder", 0x4E31)
    seen, stop = _with_rollbacks(engine)
    try:
        response = await client.post(V1.path(AuthRoutes.LOGIN), headers=nginx_headers(cert))
    finally:
        stop()
    assert response.status_code == 200, response.text
    write = _transaction_of(seen, "INSERT INTO user_sessions")
    assert write[0].startswith("SELECT users.") and "FROM users WHERE users.id = ?" in write[0], write
    assert "user_sessions" not in write[0], write[0]


def _locking_statement(monkeypatch, module, call):
    """The statement module.lock_first was asked to run first, compiled for MariaDB; call stops right after it"""
    import asyncio
    from sqlalchemy.dialects import mysql
    from app.backend.utils.row_locks import locking
    taken = []

    async def record(session, query, shared=False):
        taken.append(locking(query, shared))
        raise LookupError("recorded")             # stop before anything else: no database needed

    monkeypatch.setattr(module, "lock_first", record)
    try:
        asyncio.run(call())
    except LookupError:
        pass
    (statement,) = taken
    return str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_account_locks_select_columns_only(monkeypatch):
    """The ORM entity User carries presence column properties (models/presence_properties.py: last_activity_at,
    last_login_at) whose subqueries read user_sessions. In a locking statement only the outer table is locked: the
    subqueries are consistent reads and open the transaction's read view inside the lock (MariaDB). Two parallel ends
    of an inactive account's sessions (both share the user lock) then met session rows changed after that view: 1020.
    The shared account locks select plain columns of users only."""
    from app.backend.auth import service, sessions
    from app.backend.auth.service import AuthService
    from app.backend.models.user import User
    end = _locking_statement(monkeypatch, sessions,
                             lambda: AuthService.expire_sessions_of_inactive_user(None, 7))
    login = _locking_statement(monkeypatch, sessions,
                               lambda: AuthService.create_session(None, User(id=7, username="u7")))
    for sql in (end, login):
        assert "LOCK IN SHARE MODE" in sql and "FOR UPDATE" not in sql, sql
        assert "user_sessions" not in sql and sql.count("SELECT") == 1, sql
        assert "is_active" in sql and "is_deleted" in sql, sql


async def test_a_login_reads_the_certificate_as_committed_when_the_session_still_holds_it(
        client, db, headers, session_maker, monkeypatch):
    """The certificate authenticate_pki loaded stays in the request session's identity map while something holds it
    (expire_on_commit=False): the re-read after the user lock must overwrite it (populate_existing), or a revocation
    committed before the lock goes unseen and the login stores a session bound to a revoked certificate"""
    from app.backend.api.v1 import V1
    from app.backend.auth.certificates import hash_data
    from app.backend.auth.service import AuthService
    from app.backend.auth.sessions import live_clause
    from app.backend.enums.routes import AuthRoutes
    from app.backend.models.user_certificate import UserCertificate
    from tests.test_pki_login import nginx_headers
    user_id, cert = await _pki_user(client, db, headers, monkeypatch, "stalecert", 0x4E32)
    held = []
    authenticate = AuthService.authenticate_pki

    async def authenticate_and_hold(session, *args, **kwargs):
        result = await authenticate(session, *args, **kwargs)
        held.extend((await session.exec(select(UserCertificate).where(UserCertificate.user_id == user_id))).all())
        return result

    monkeypatch.setattr(AuthService, "authenticate_pki", staticmethod(authenticate_and_hold))

    async def change():
        async with session_maker() as other:
            fingerprint_hash = await hash_data(nginx_headers(cert)["X-SSL-Client-Fingerprint"])
            row = (await other.exec(select(UserCertificate).where(
                UserCertificate.fingerprint_hash == fingerprint_hash))).one()
            row.revoked_at = datetime.now(timezone.utc)
            other.add(row)
            await other.commit()

    _before_the_session_is_stored(monkeypatch, change)
    response = await client.post(V1.path(AuthRoutes.LOGIN), headers=nginx_headers(cert))
    async with session_maker() as fresh:
        live = (await fresh.exec(select(UserSession).where(UserSession.user_id == user_id, live_clause()))).all()
    assert held, "the stale copy has to be held by the session"
    assert response.status_code == 401 and response.json()["code"] == "certificate_revoked", response.text
    assert live == []


async def test_a_refresh_that_met_a_reactivated_account_twice_answers_session_expired(client, db, session_maker,
                                                                                      monkeypatch):
    """The refresh runs again once when the account was reactivated between its rollback and the lock of the end.
    The account flapped again before the second attempt and was active once more at its end: the answer is not
    account_deactivated (the account is active as committed), it is session_expired, declared on refresh."""
    from app.backend.api.v1 import V1
    from app.backend.api.v1 import auth as auth_routes
    from app.backend.auth import sessions
    from app.backend.auth.service import AuthService
    from app.backend.enums.routes import AuthRoutes
    from app.backend.models.user import User
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh = AuthService.session_tokens(user.id, row)[1]
    await _deactivate(db, user)

    def set_active(value):
        async def change():
            async with session_maker() as other:
                stored = (await other.exec(select(User).where(User.id == user.id))).one()
                stored.is_active = value
                other.add(stored)
                await other.commit()
        return change

    writes, ends = [], []
    claim_write, end_lock = auth_routes.begin_session_write, sessions.begin_account_session_write

    async def refresh_write(session, user_id):
        writes.append(user_id)
        if len(writes) == 2:
            await set_active(False)()            # deactivated again before the second attempt's lock
        return await claim_write(session, user_id)

    async def end_write(session, user_id):
        ends.append(user_id)
        await set_active(True)()                 # reactivated before the end's lock, both times
        return await end_lock(session, user_id)

    monkeypatch.setattr(auth_routes, "begin_session_write", refresh_write)
    monkeypatch.setattr(sessions, "begin_account_session_write", end_write)
    response = await client.post(V1.path(AuthRoutes.REFRESH), json={"refresh_token": refresh})
    assert (len(writes), len(ends)) == (2, 2)
    assert response.status_code == 401 and response.json()["code"] == "session_expired", response.text
    assert await _ended_and_logged(session_maker, user.id) == ([], [])
