"""Production login with the client certificate as nginx forwards it (X-SSL-Client-*), and its sessions."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.general import AppEnvironment
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.models.role import Role
from tests.helpers import make_admin

LOGIN = V1.path(AuthRoutes.LOGIN)
REFRESH = V1.path(AuthRoutes.REFRESH)
ME = V1.path(AuthRoutes.ME)
CREATE_USER = V1.path(UserRoutes.ROOT)
USER = V1.path(UserRoutes.USER_BY_ID)


def make_certificate(common_name: str, serial: int) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )


def pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def nginx_headers(cert: x509.Certificate) -> dict:
    """What nginx sets after ssl_verify_client: $ssl_client_s_dn (RFC 2253), $ssl_client_serial (OpenSSL
    i2a_ASN1_INTEGER: two upper-case hex digits per byte), $ssl_client_fingerprint (SHA-1 of the DER, lower case)"""
    serial = cert.serial_number
    return {
        "X-SSL-Client-Verify": "SUCCESS",
        "X-SSL-Client-DN": cert.subject.rfc4514_string(),
        "X-SSL-Client-Serial": serial.to_bytes((serial.bit_length() + 7) // 8, "big").hex().upper(),
        "X-SSL-Client-Fingerprint": hashlib.sha1(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
    }


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings.app, "environment", AppEnvironment.PRODUCTION.value)


async def _create_user(client, db, headers, username: str, cert: x509.Certificate) -> int:
    admin = await make_admin(db)
    role_id = (await db.exec(select(Role).where(Role.name == "admin"))).one().id
    created = await client.post(
        CREATE_USER, headers=headers(admin),
        files={"certificate": ("cert.pem", pem(cert), "application/x-pem-file")},
        data={"username": username, "username_alias": "Alias", "role_ids": [role_id]},
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


async def _tokens(client, cert: x509.Certificate) -> dict:
    response = await client.post(LOGIN, headers=nginx_headers(cert))
    assert response.status_code == 200, response.text
    return response.json()


async def _login(client, cert: x509.Certificate) -> dict:
    """Headers of later requests: the token, plus the certificate headers nginx adds to every request"""
    tokens = await _tokens(client, cert)
    return {**nginx_headers(cert), "Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("serial", [0x0A1B2C, 0x01, 0x1000, x509.random_serial_number()])
async def test_login_with_the_serial_as_nginx_writes_it(client, db, headers, production, serial):
    """nginx sends 0x0A1B2C as "0A1B2C", the certificate upload stored "a1b2c": the login matches both forms"""
    cert = make_certificate("cn-serial", serial)
    await _create_user(client, db, headers, "serialuser", cert)
    me = await client.get(ME, headers=await _login(client, cert))
    assert me.status_code == 200 and me.json()["username"] == "serialuser"


@pytest.mark.asyncio
async def test_unverified_certificate_is_refused(client, db, headers, production):
    cert = make_certificate("cn-unverified", 0x1234)
    await _create_user(client, db, headers, "unverified", cert)
    response = await client.post(LOGIN, headers={**nginx_headers(cert), "X-SSL-Client-Verify": "FAILED:certificate has expired"})
    assert response.status_code == 401 and response.json()["code"] == "certificate_verification_failed"


@pytest.mark.asyncio
async def test_new_certificate_ends_the_sessions_of_the_old_one(client, db, headers, production):
    old = make_certificate("cn-replace", 0x2222)
    user_id = await _create_user(client, db, headers, "replaced", old)
    old_session = await _login(client, old)
    assert (await client.get(ME, headers=old_session)).status_code == 200

    # the same certificate again changes nothing
    admin = await make_admin(db)
    same = await client.patch(USER.format(user_id=user_id), headers=headers(admin),
                              files={"certificate": ("cert.pem", pem(old), "application/x-pem-file")})
    assert same.status_code == 200, same.text
    assert (await client.get(ME, headers=old_session)).status_code == 200

    new = make_certificate("cn-replace-new", 0x3333)
    replaced = await client.patch(USER.format(user_id=user_id), headers=headers(admin),
                                  files={"certificate": ("cert.pem", pem(new), "application/x-pem-file")})
    assert replaced.status_code == 200, replaced.text
    ended = await client.get(ME, headers=old_session)
    assert ended.status_code == 401 and ended.json()["code"] == "session_expired"

    refused = await client.post(LOGIN, headers=nginx_headers(old))
    assert refused.status_code == 401 and refused.json()["code"] == "certificate_not_recognized"
    assert (await client.get(ME, headers=await _login(client, new))).status_code == 200


@pytest.mark.asyncio
async def test_session_is_bound_to_its_certificate(client, db, headers, production):
    """RFC 8705 style: a token works only with the certificate the session was opened with"""
    own = make_certificate("cn-bound", 0x4444)
    other = make_certificate("cn-other", 0x5555)
    await _create_user(client, db, headers, "bound", own)
    await _create_user(client, db, headers, "otheruser", other)
    tokens = await _tokens(client, own)
    bearer = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (await client.get(ME, headers={**nginx_headers(own), **bearer})).status_code == 200
    stolen = await client.get(ME, headers={**nginx_headers(other), **bearer})
    assert stolen.status_code == 401 and stolen.json()["code"] == "token_certificate_mismatch"
    without = await client.get(ME, headers=bearer)
    assert without.status_code == 401 and without.json()["code"] == "token_certificate_mismatch", "no certificate, no use"

    refresh = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=nginx_headers(other))
    assert refresh.status_code == 401 and refresh.json()["code"] == "token_certificate_mismatch"
    refresh = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=nginx_headers(own))
    assert refresh.status_code == 200, refresh.text
    # a refused attempt does not end the session: the owner keeps working
    assert (await client.get(ME, headers={**nginx_headers(own), **bearer})).status_code == 200

