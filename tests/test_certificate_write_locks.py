"""Certificate writes lock the target user before check_target_user (owner decisions 2026-10-29, audit L2).

check_target_user reads the assignments of the target user. Every writer of a user's assignments locks the user row
first, so the check holds only when it runs after the certificate write took that lock: otherwise an assignment
added meanwhile (which does not change the users row, so MariaDB raises no 1020) leaves a stale check standing."""
from sqlalchemy import event

from app.backend.api.v1 import V1
from app.backend.enums.general import CertificateSource
from app.backend.enums.routes import UserRoutes
from tests.helpers import issue, make_admin, make_certificate_row, make_user
from tests.pki_helpers import legacy_root, make_leaf, pem


def _statements(engine):
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


def _assert_locked_then_checked(write):
    """The write begins with the user row and reads the target's assignments (check_target_user) inside it"""
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert any("FROM role_assignments" in statement for statement in write), write


async def _target(db):
    return await make_user(db, perms=["post.view"])


async def test_issue_checks_the_target_after_locking_it(client, db, headers, engine, pki):
    admin, user = await make_admin(db), await _target(db)
    seen, stop = _statements(engine)
    try:
        response = await issue(client, headers, admin, user)
    finally:
        stop()
    assert response.status_code == 201, response.text
    _assert_locked_then_checked(_write_of(seen, "INSERT INTO user_certificates"))


async def test_upload_checks_the_target_after_locking_it(client, db, headers, engine):
    admin, user = await make_admin(db), await _target(db)
    cert, _ = make_leaf(legacy_root(), label="Plain")
    seen, stop = _statements(engine)
    try:
        response = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_UPLOAD, user_id=user.id),
                                     headers=headers(admin), data={"label": "Office PC"},
                                     files={"file": ("cert.pem", pem(cert), "application/x-pem-file")})
    finally:
        stop()
    assert response.status_code == 201, response.text
    _assert_locked_then_checked(_write_of(seen, "INSERT INTO user_certificates"))


async def test_export_checks_the_target_after_locking_it(client, db, headers, engine, pki):
    admin, user = await make_admin(db), await _target(db)
    certificate_id = (await issue(client, headers, admin, user)).json()["certificate"]["id"]
    seen, stop = _statements(engine)
    try:
        response = await client.post(
            V1.path(UserRoutes.USER_CERTIFICATE_EXPORT, user_id=user.id, certificate_id=certificate_id),
            headers=headers(admin))
    finally:
        stop()
    assert response.status_code == 200, response.text
    _assert_locked_then_checked(_write_of(seen, "UPDATE user_certificates"))


async def test_revoke_takes_the_administrators_lock_then_checks_the_target_after_locking_it(client, db, headers, engine):
    admin, user = await make_admin(db), await _target(db)
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
    # A revocation may take an effective administrator away: lock_administrators first, one order for such writes
    assert write[0].startswith("SELECT roles.id") and "roles.builtin_key = ?" in write[0], write
    _assert_locked_then_checked(write[1:])
