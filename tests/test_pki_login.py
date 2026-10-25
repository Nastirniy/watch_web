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
from app.backend.auth.certificates import hash_data, parse_cn
from app.backend.config import settings
from app.backend.enums.general import AppEnvironment
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import UserCertificate
from tests.helpers import make_admin, make_certificate_row

LOGIN = V1.path(AuthRoutes.LOGIN)
REFRESH = V1.path(AuthRoutes.REFRESH)
ME = V1.path(AuthRoutes.ME)
CREATE_USER = V1.path(UserRoutes.ROOT)


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


_RFC2253_SPECIALS = ',+"\\<>;'


def openssl_rfc2253(name: x509.Name) -> str:
    """$ssl_client_s_dn as nginx writes it (OpenSSL X509_NAME_print_ex, XN_FLAG_RFC2253): RDNs last first (the
    attributes of a multi-valued RDN too, XN_FLAG_DN_REV reverses every entry), the
    specials of RFC 2253 and a leading '#' or space escaped with a backslash, every byte of a character above 0x7F
    (and every control character) as \\XX. rfc4514_string() leaves non-ASCII characters as they are."""
    def escape(value: str) -> str:
        out = []
        for index, char in enumerate(value):
            if ord(char) > 0x7E or ord(char) < 0x20:
                out.extend(f"\\{byte:02X}" for byte in char.encode("utf-8"))
            elif char in _RFC2253_SPECIALS or (index == 0 and char in "# ") or (index == len(value) - 1 and char == " "):
                out.append("\\" + char)
            else:
                out.append(char)
        return "".join(out)
    return ",".join("+".join(f"{attribute.rfc4514_attribute_name}={escape(attribute.value)}" for attribute in reversed(list(rdn)))
                    for rdn in reversed(name.rdns))


# openssl x509 -noout -subject -nameopt RFC2253 of a certificate with CN "Ivan Petrenko" written in Cyrillic
CYRILLIC_CN = "\u0406\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u0435\u043d\u043a\u043e"
CYRILLIC_DN = r"CN=\D0\86\D0\B2\D0\B0\D0\BD \D0\9F\D0\B5\D1\82\D1\80\D0\B5\D0\BD\D0\BA\D0\BE"


def nginx_headers(cert: x509.Certificate) -> dict:
    """What nginx sets after ssl_verify_client: $ssl_client_s_dn (OpenSSL RFC 2253 form), $ssl_client_serial (OpenSSL
    i2a_ASN1_INTEGER: two upper-case hex digits per byte), $ssl_client_fingerprint (SHA-1 of the DER, lower case)"""
    serial = cert.serial_number
    return {
        "X-SSL-Client-Verify": "SUCCESS",
        "X-SSL-Client-DN": openssl_rfc2253(cert.subject),
        "X-SSL-Client-Serial": serial.to_bytes((serial.bit_length() + 7) // 8, "big").hex().upper(),
        "X-SSL-Client-Fingerprint": hashlib.sha1(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
    }


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings.app, "environment", AppEnvironment.PRODUCTION.value)


async def _create_user(client, db, headers, username: str, cert: x509.Certificate) -> int:
    admin = await make_admin(db)
    created = await client.post(
        CREATE_USER, headers=headers(admin),
        files={"certificate": ("cert.pem", pem(cert), "application/x-pem-file")},
        data={"username": username, "username_alias": "Alias", "user_type": "system_administrator"},
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


async def _certificate_row(db, cert: x509.Certificate) -> UserCertificate:
    fingerprint_hash = await hash_data(nginx_headers(cert)["X-SSL-Client-Fingerprint"])
    return (await db.exec(select(UserCertificate).where(UserCertificate.fingerprint_hash == fingerprint_hash))).one()


@pytest.mark.asyncio
async def test_a_user_logs_in_with_each_of_its_certificates(client, db, headers, production):
    """Several certificates per user (a renewal overlaps): each logs in as the user; revoking the old one refuses its
    login and leaves the new one's session"""
    old = make_certificate("cn-replace", 0x2222)
    user_id = await _create_user(client, db, headers, "replaced", old)
    user = (await db.exec(select(User).where(User.id == user_id))).one()
    new = make_certificate("cn-replace-new", 0x3333)
    await make_certificate_row(db, user, cert=new)

    old_session = await _login(client, old)
    new_session = await _login(client, new)
    for device in (old_session, new_session):
        me = await client.get(ME, headers=device)
        assert me.status_code == 200 and me.json()["username"] == "replaced", me.text

    row = await _certificate_row(db, old)
    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    await db.commit()
    refused = await client.post(LOGIN, headers=nginx_headers(old))
    assert refused.status_code == 401 and refused.json()["code"] == "certificate_revoked"
    assert (await client.get(ME, headers=new_session)).status_code == 200


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
    # the refused attempt retired nothing: the refresh token still works once, with its own certificate
    refresh = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=nginx_headers(own))
    assert refresh.status_code == 200, refresh.text
    replay = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=nginx_headers(own))
    assert replay.status_code == 401 and replay.json()["code"] == "invalid_refresh_token"
    # neither a refused attempt nor a replay ends the session: the owner keeps working
    assert (await client.get(ME, headers={**nginx_headers(own), **bearer})).status_code == 200


# ------------------------------------------------------------------ the subject DN as nginx writes it
def test_the_test_helper_writes_the_dn_as_openssl_does():
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CYRILLIC_CN)])
    assert openssl_rfc2253(name) == CYRILLIC_DN


@pytest.mark.asyncio
@pytest.mark.parametrize("dn, cn", [
    (CYRILLIC_DN, CYRILLIC_CN),
    (CYRILLIC_DN + r",O=\D0\A2\D0\9E\D0\92,C=UA", CYRILLIC_CN),
    (r"CN=Doe\, John,OU=Unit\+One,O=Example", "Doe, John"),
    (r"UID=7+CN=a\+b\; c\\d\"e\<f\>,O=x", 'a+b; c\\d"e<f>'),
    (r"CN=\#hash and space\ ,O=x", "#hash and space "),
    ("OU=Unit,CN=Plain Name,O=Org", "Plain Name"),
    ("CN=a=b,O=x", "a=b"),
    ("/C=UA/O=Org/CN=legacy-name", "legacy-name"),
    ("O=Org,C=UA", None),
    ("", None),
])
async def test_parse_cn_reads_the_openssl_rfc2253_form(dn, cn):
    assert await parse_cn(dn) == cn


@pytest.mark.asyncio
async def test_parse_cn_refuses_bytes_that_are_not_utf8():
    assert await parse_cn(r"CN=\D0\D0,O=x") is None


@pytest.mark.asyncio
async def test_login_with_a_cyrillic_common_name(client, db, headers, production):
    """nginx escapes every byte above 0x7F: the login reads the CN the certificate upload stored"""
    cert = make_certificate(CYRILLIC_CN, 0x6666)
    await _create_user(client, db, headers, "cyrillic", cert)
    assert nginx_headers(cert)["X-SSL-Client-DN"] == CYRILLIC_DN
    me = await client.get(ME, headers=await _login(client, cert))
    assert me.status_code == 200 and me.json()["username"] == "cyrillic"


# ------------------------------------------------------------------ a session bound to a revoked certificate
@pytest.mark.asyncio
async def test_a_session_of_a_revoked_certificate_ends_on_its_next_request(client, db, headers, production):
    """A login racing a revocation: the revocation ran before the new session was stored, so the session is bound
    to a revoked certificate. Its next request or refresh ends it (revoked, certificate_revoked)."""
    old = make_certificate("cn-race", 0x7777)
    user_id = await _create_user(client, db, headers, "racer", old)
    on_request = await _tokens(client, old)
    on_refresh = await _tokens(client, old)

    # the revocation as it lands after the logins: these sessions stayed live
    row = await _certificate_row(db, old)
    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    await db.commit()

    me = await client.get(ME, headers={**nginx_headers(old), "Authorization": f"Bearer {on_request['access_token']}"})
    assert me.status_code == 401 and me.json()["code"] == "token_certificate_mismatch"
    refresh = await client.post(REFRESH, json={"refresh_token": on_refresh["refresh_token"]}, headers=nginx_headers(old))
    assert refresh.status_code == 401 and refresh.json()["code"] == "token_certificate_mismatch"

    db.expire_all()
    rows = (await db.exec(select(UserSession).where(UserSession.user_id == user_id))).all()
    assert len(rows) == 2
    assert all((row.end_reason, row.revoke_cause) == ("revoked", "certificate_revoked") and row.ended_at for row in rows)
    again = await client.get(ME, headers={**nginx_headers(old), "Authorization": f"Bearer {on_request['access_token']}"})
    assert again.status_code == 401 and again.json()["code"] == "session_expired"

