"""user_certificates: POST /users/ registers an uploaded certificate, the login and the sessions follow the rows
(revoked, expired, legacy, configured), the development seed and init_user write rows too."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.certificates import CN_SCHEME_CURRENT, CN_SCHEME_LEGACY, hash_cn, hash_data
from app.backend.config import settings
from app.backend.enums.general import CertificateSource, UserType
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.utils.datetime_utils import assume_utc
from tests.helpers import make_admin, make_certificate_row, make_user
from tests.pki_helpers import client_ca, legacy_root, make_leaf, pem
from tests.test_pki_login import _login, make_certificate, nginx_headers, production  # noqa: F401

CREATE_USER = V1.path(UserRoutes.ROOT)
USER = V1.path(UserRoutes.USER_BY_ID)
LOGIN = V1.path(AuthRoutes.LOGIN)
ME = V1.path(AuthRoutes.ME)


def certificate_file(cert) -> dict:
    return {"certificate": ("cert.pem", pem(cert), "application/x-pem-file")}


async def create(client, headers, admin, username: str, cert=None, **data):
    return await client.post(
        CREATE_USER, headers=headers(admin), files=certificate_file(cert) if cert is not None else None,
        data={"username": username, "username_alias": "Alias", "user_type": "viewer", **data},
    )


async def rows_of(db, user_id: int) -> list:
    """The certificate rows of the user as stored now (the routes write them through their own sessions)"""
    result = await db.exec(select(UserCertificate).where(UserCertificate.user_id == user_id)
                           .order_by(UserCertificate.id).execution_options(populate_existing=True))
    return result.all()


async def user_named(db, username: str):
    return (await db.exec(select(User).where(User.username == username))).first()


def fingerprint(cert) -> str:
    return cert.fingerprint(hashes.SHA1()).hex()


# ------------------------------------------------------------------ POST /users/ and PATCH /users/{id}
@pytest.mark.asyncio
async def test_create_user_with_certificate_writes_an_uploaded_row(client, db, headers):
    admin = await make_admin(db)
    cert, _ = make_leaf(legacy_root(), label="Upload CN")
    response = await create(client, headers, admin, "uploaded", cert, certificate_label="Office PC")
    assert response.status_code == 200, response.text

    (row,) = await rows_of(db, response.json()["id"])
    assert (row.source, row.label, row.subject_cn) == (CertificateSource.UPLOADED.value, "Office PC", "Upload CN")
    assert row.serial == format(cert.serial_number, "x") and row.serial == row.serial.lower()
    assert assume_utc(row.not_after) == cert.not_valid_after_utc
    assert assume_utc(row.not_before) == cert.not_valid_before_utc
    assert row.ca_issued is False and row.revoked_at is None and row.export_count == 0 and row.files_dir is None
    assert row.created_by == admin.id
    assert row.cn_hash == await hash_cn("Upload CN") and row.cn_hash_scheme == CN_SCHEME_CURRENT
    assert row.serial_hash == await hash_data(row.serial)
    assert row.fingerprint_hash == await hash_data(fingerprint(cert))


@pytest.mark.asyncio
async def test_create_user_without_certificate(client, db, headers):
    admin = await make_admin(db)
    response = await create(client, headers, admin, "no_certificate")
    assert response.status_code == 200, response.text
    assert await rows_of(db, response.json()["id"]) == []


@pytest.mark.asyncio
async def test_label_defaults_to_the_cn_and_is_validated(client, db, headers):
    admin = await make_admin(db)
    cert, _ = make_leaf(legacy_root(), label="Default Label CN")
    response = await create(client, headers, admin, "default_label", cert)
    assert response.status_code == 200, response.text
    (row,) = await rows_of(db, response.json()["id"])
    assert row.label == "Default Label CN"

    other, _ = make_leaf(legacy_root(), label="Other")
    for label in ("a" * 65, "bell\x07", "   "):
        refused = await create(client, headers, admin, "bad_label", other, certificate_label=label)
        assert refused.status_code == 422, refused.text
        (error,) = refused.json()["errors"]
        assert (error["code"], error["pointer"]) == ("certificate_label_invalid", "#/certificate_label")
    assert await user_named(db, "bad_label") is None

    trimmed = await create(client, headers, admin, "trimmed_label", other, certificate_label="  " + "b" * 64 + " ")
    assert trimmed.status_code == 200, trimmed.text
    assert (await rows_of(db, trimmed.json()["id"]))[0].label == "b" * 64


@pytest.mark.asyncio
async def test_upload_signed_by_the_configured_ca_is_ca_issued(client, db, headers, tmp_path, monkeypatch):
    ca_file = tmp_path / "client-ca.crt"
    ca_file.write_bytes(pem(client_ca()[0]))
    monkeypatch.setattr(settings.pki, "ca_cert_path", str(ca_file))
    admin = await make_admin(db)
    ours, _ = make_leaf(client_ca(), label="Ours")
    foreign, _ = make_leaf(legacy_root(), label="Foreign")
    for username, cert, expected in (("ours", ours, True), ("foreign", foreign, False)):
        response = await create(client, headers, admin, username, cert)
        assert response.status_code == 200, response.text
        (row,) = await rows_of(db, response.json()["id"])
        assert row.ca_issued is expected, username


@pytest.mark.asyncio
async def test_without_a_configured_ca_nothing_is_ca_issued(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.pki, "ca_cert_path", "")
    admin = await make_admin(db)
    ours, _ = make_leaf(client_ca(), label="Ours unconfigured")
    response = await create(client, headers, admin, "unconfigured", ours)
    assert response.status_code == 200, response.text
    assert (await rows_of(db, response.json()["id"]))[0].ca_issued is False


@pytest.mark.asyncio
async def test_same_certificate_twice_is_taken(client, db, headers):
    admin = await make_admin(db)
    cert, _ = make_leaf(legacy_root(), label="Shared device")
    first = await create(client, headers, admin, "first_owner", cert)
    assert first.status_code == 200, first.text
    second = await create(client, headers, admin, "second_owner", cert)
    assert second.status_code == 400, second.text
    assert second.json()["code"] == "certificate_taken"
    assert second.json()["title"] == "Certificate already registered"
    assert await user_named(db, "second_owner") is None

    # the same name in another certificate is another certificate
    same_name, _ = make_leaf(legacy_root(), label="Shared device")
    third = await create(client, headers, admin, "third_owner", same_name)
    assert third.status_code == 200, third.text


@pytest.mark.asyncio
async def test_invalid_certificate_creates_no_user(client, db, headers):
    admin = await make_admin(db)
    response = await client.post(
        CREATE_USER, headers=headers(admin),
        files={"certificate": ("cert.pem", b"not a certificate", "application/x-pem-file")},
        data={"username": "invalid_cert", "username_alias": "Alias", "user_type": "viewer"},
    )
    assert response.status_code == 400 and response.json()["code"] == "certificate_invalid", response.text
    assert await user_named(db, "invalid_cert") is None


@pytest.mark.asyncio
async def test_patch_user_no_longer_takes_a_certificate(client, db, headers):
    admin = await make_admin(db)
    cert, _ = make_leaf(legacy_root(), label="Kept")
    created = await create(client, headers, admin, "patched", cert)
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    before = [(row.id, row.fingerprint_hash) for row in await rows_of(db, user_id)]

    replacement, _ = make_leaf(legacy_root(), label="Replacement")
    changed = await client.patch(USER.format(user_id=user_id), headers=headers(admin),
                                 files=certificate_file(replacement), data={"username_alias": "Changed"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["username_alias"] == "Changed"
    only_file = await client.patch(USER.format(user_id=user_id), headers=headers(admin), files=certificate_file(replacement))
    assert only_file.status_code == 200, only_file.text
    assert [(row.id, row.fingerprint_hash) for row in await rows_of(db, user_id)] == before


# ------------------------------------------------------------------ login and sessions
@pytest.mark.asyncio
async def test_revoked_certificate_login_is_401_certificate_revoked(client, db, headers, production):
    user = await make_user(db)
    cert = make_certificate("cn-revoked", 0x9101)
    await make_certificate_row(db, user, cert=cert, revoked_at=datetime.now(timezone.utc),
                               revocation_reason="key_compromise")
    response = await client.post(LOGIN, headers=nginx_headers(cert))
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "certificate_revoked"


@pytest.mark.asyncio
async def test_expired_certificate_login_is_not_recognized(client, db, headers, production):
    admin = await make_admin(db)
    cert, _ = make_leaf(client_ca(), label="Expired device", days=30,
                        now=datetime.now(timezone.utc) - timedelta(days=40))
    created = await create(client, headers, admin, "expired_owner", cert)
    assert created.status_code == 200, created.text   # an expired file is accepted (and listed as expired)
    response = await client.post(LOGIN, headers=nginx_headers(cert))
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "certificate_not_recognized"


@pytest.mark.asyncio
async def test_session_of_a_revoked_certificate_ends(client, db, headers, production):
    user = await make_user(db)
    cert = make_certificate("cn-session", 0x9103)
    other = make_certificate("cn-session-other", 0x9104)
    row = await make_certificate_row(db, user, cert=cert)
    await make_certificate_row(db, user, cert=other)
    device = await _login(client, cert)
    other_device = await _login(client, other)
    assert (await client.get(ME, headers=device)).status_code == 200

    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    await db.commit()

    ended = await client.get(ME, headers=device)
    assert ended.status_code == 401 and ended.json()["code"] == "token_certificate_mismatch", ended.text
    assert (await client.get(ME, headers=other_device)).status_code == 200, "the user's other device stays logged in"

    sessions = (await db.exec(select(UserSession).where(UserSession.user_id == user.id)
                              .execution_options(populate_existing=True))).all()
    by_certificate = {s.certificate_fingerprint_hash: s for s in sessions}
    revoked = by_certificate[row.fingerprint_hash]
    assert (revoked.end_reason, revoked.revoke_cause) == ("revoked", "certificate_revoked") and revoked.ended_at
    assert by_certificate[await hash_data(fingerprint(other))].ended_at is None


@pytest.mark.asyncio
async def test_legacy_and_configured_rows_log_in(client, db, headers, production):
    legacy_user = await make_user(db, username="legacy_owner")
    legacy_cert = make_certificate("Legacy-CN", 0x9105)
    # a row from before 019: the name hashed with hash_data (scheme 1), no plain values
    await make_certificate_row(db, legacy_user, CertificateSource.LEGACY, cert=legacy_cert,
                               cn_hash=await hash_data("Legacy-CN"), cn_hash_scheme=CN_SCHEME_LEGACY)
    configured_user = await make_admin(db)
    configured_cert = make_certificate("configured-cn", 0x9106)
    configured = await make_certificate_row(db, configured_user, CertificateSource.CONFIGURED, cert=configured_cert)
    assert (configured.label, configured.subject_cn, configured.not_after) == ("", None, None)

    for user, cert in ((legacy_user, legacy_cert), (configured_user, configured_cert)):
        me = await client.get(ME, headers=await _login(client, cert))
        assert me.status_code == 200 and me.json()["username"] == user.username, me.text


# ------------------------------------------------------------------ seed and CLI
@pytest.mark.asyncio
async def test_development_seed_writes_a_configured_row(client, db, session_maker, monkeypatch):
    from app.backend.bootstrap import seed

    async def nothing():
        return None

    async def maker():
        return session_maker

    monkeypatch.setattr(seed, "create_database_if_not_exists", nothing)
    monkeypatch.setattr(seed, "get_session_maker", maker)
    monkeypatch.setattr(settings.admin, "username", "seeded_admin")
    monkeypatch.setattr(settings.admin, "pki_certificate_cn", "seeded-admin@example.local")
    monkeypatch.setattr(settings.admin, "pki_certificate_serial", "0A1B2C3D")
    monkeypatch.setattr(settings.admin, "pki_certificate_fingerprint", "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01")
    await seed.seed_database()

    admin = await user_named(db, "seeded_admin")
    (row,) = await rows_of(db, admin.id)
    assert (row.source, row.label, row.subject_cn, row.serial, row.created_by) == (
        CertificateSource.CONFIGURED.value, "", None, None, None)
    assert row.cn_hash == await hash_cn("seeded-admin@example.local") and row.cn_hash_scheme == CN_SCHEME_CURRENT
    assert row.serial_hash == await hash_data("0A1B2C3D")
    assert row.fingerprint_hash == await hash_data("AB:CD:EF:01:23:45:67:89:AB:CD:EF:01")

    login = await client.post(LOGIN, json={"certificate_cn": "seeded-admin@example.local",
                                           "certificate_serial": "0A1B2C3D",
                                           "certificate_fingerprint": "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01"})
    assert login.status_code == 200, login.text
    await seed.seed_database()   # idempotent: the admin exists, no second row
    assert len(await rows_of(db, admin.id)) == 1


@pytest.mark.asyncio
async def test_init_user_with_and_without_a_certificate(db, session_maker, monkeypatch, tmp_path):
    from app.backend.cli import init_user

    async def nothing():
        return None

    async def maker():
        return session_maker

    monkeypatch.setattr(init_user, "create_database_if_not_exists", nothing)
    monkeypatch.setattr(init_user, "get_session_maker", maker)

    await init_user.create_user("cli_admin", "CLI Admin", None, UserType.ADMIN)
    admin = await user_named(db, "cli_admin")
    assert admin is not None and await rows_of(db, admin.id) == []

    cert, _ = make_leaf(legacy_root(), label="CLI device")
    cert_file = tmp_path / "device.pem"
    cert_file.write_bytes(pem(cert))
    await init_user.create_user("cli_viewer", "CLI Viewer", str(cert_file), UserType.VIEWER, label="CLI laptop")
    viewer = await user_named(db, "cli_viewer")
    (row,) = await rows_of(db, viewer.id)
    assert (row.source, row.label, row.subject_cn, row.created_by) == (
        CertificateSource.UPLOADED.value, "CLI laptop", "CLI device", None)

    with pytest.raises(SystemExit):   # the same certificate again
        await init_user.create_user("cli_again", "CLI Again", str(cert_file), UserType.VIEWER)
    assert await user_named(db, "cli_again") is None
    with pytest.raises(SystemExit):   # a label the API would refuse
        await init_user.create_user("cli_label", "CLI Label", str(cert_file), UserType.VIEWER, label="x" * 65)
    assert await user_named(db, "cli_label") is None
