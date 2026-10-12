"""Certificate-name hash schemes: scheme 1 (hash_data, rows made before 019) and scheme 2 (hash_cn)."""
import hashlib
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.certificates import CertificateParser, hash_cn, hash_data, parse_cn
from app.backend.config import settings
from app.backend.enums.general import AppEnvironment
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.models.user import User
from tests.helpers import make_admin
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, run_migrate
from tests.test_pki_login import _create_user, _login, make_certificate, openssl_rfc2253, pem, production  # noqa: F401

LOGIN = V1.path(AuthRoutes.LOGIN)
ME = V1.path(AuthRoutes.ME)
CREATE_USER = V1.path(UserRoutes.ROOT)


async def _row(db, username: str) -> User:
    return (await db.exec(select(User).where(User.username == username))).one()


async def _post_certificate(client, db, headers, username: str, cert: x509.Certificate):
    admin = await make_admin(db)
    return await client.post(
        CREATE_USER, headers=headers(admin),
        files={"certificate": ("cert.pem", pem(cert), "application/x-pem-file")},
        data={"username": username, "username_alias": "Alias", "user_type": "admin"},
    )


async def _make_scheme_1(db, username: str, name: str) -> None:
    row = await _row(db, username)
    row.pki_certificate_cn_hash = await hash_data(name)
    row.pki_certificate_cn_hash_scheme = 1
    db.add(row)
    await db.commit()


def _dev_login(cert: x509.Certificate, cn: str) -> dict:
    info = CertificateParser.parse_certificate(pem(cert))
    return {
        "certificate_cn": cn,
        "certificate_serial": info["pki_certificate_serial"],
        "certificate_fingerprint": info["pki_certificate_fingerprint"],
    }


def multi_cn_certificate(first: str, second: str, serial: int, one_rdn: bool = False) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    country = x509.NameAttribute(NameOID.COUNTRY_NAME, "UA")
    names = [x509.NameAttribute(NameOID.COMMON_NAME, first), x509.NameAttribute(NameOID.COMMON_NAME, second)]
    if one_rdn:
        name = x509.Name([x509.RelativeDistinguishedName([country]), x509.RelativeDistinguishedName(names)])
    else:
        name = x509.Name([country, *names])
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(serial).not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))


@pytest.mark.asyncio
async def test_hash_cn_keeps_case_and_dashes_and_normalizes_unicode():
    assert await hash_cn("Post-Kyiv") != await hash_cn("postkyiv")
    assert await hash_cn("Post-Kyiv") != await hash_cn("post-kyiv")
    assert await hash_cn("  Post-Kyiv ") == await hash_cn("Post-Kyiv")
    composed = "é"
    assert await hash_cn(composed) == await hash_cn(unicodedata.normalize("NFD", composed))
    assert await hash_cn("a:b") == hashlib.sha256(b"a:b").hexdigest()
    assert await hash_data("Post-Kyiv") == await hash_data("postkyiv")


@pytest.mark.asyncio
async def test_names_differing_by_case_and_dashes_are_two_certificates(client, db, headers):
    await _create_user(client, db, headers, "kyiv-a", make_certificate("Post-Kyiv", 0xA1))
    await _create_user(client, db, headers, "kyiv-b", make_certificate("postkyiv", 0xA2))
    first, second = await _row(db, "kyiv-a"), await _row(db, "kyiv-b")
    assert first.pki_certificate_cn_hash_scheme == second.pki_certificate_cn_hash_scheme == 2
    assert first.pki_certificate_cn_hash == await hash_cn("Post-Kyiv")


@pytest.mark.asyncio
async def test_the_same_name_is_still_taken(client, db, headers):
    await _create_user(client, db, headers, "same-a", make_certificate("Post-Kyiv", 0xA3))
    response = await _post_certificate(client, db, headers, "same-b", make_certificate("Post-Kyiv", 0xA4))
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_a_scheme_1_row_collides_under_its_own_rule(client, db, headers, production):
    old = make_certificate("Old-Name", 0xB1)
    await _create_user(client, db, headers, "old-scheme", old)
    await _make_scheme_1(db, "old-scheme", "Old-Name")
    me = await client.get(ME, headers=await _login(client, old))
    assert me.status_code == 200 and me.json()["username"] == "old-scheme"
    # the old row stored hash_data("Old-Name"): a name equal under that rule is the same certificate name
    refused = await _post_certificate(client, db, headers, "third", make_certificate("oldname", 0xB3))
    assert refused.status_code == 400, refused.text
    # a scheme 2 name is compared only with scheme 2 rows
    allowed = await _post_certificate(client, db, headers, "fourth", make_certificate("Other", 0xB4))
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_a_scheme_2_row_logs_in_in_production_and_development(client, db, headers, production, monkeypatch):
    cert = make_certificate("New-Name", 0xC1)
    await _create_user(client, db, headers, "scheme-two", cert)
    assert (await _row(db, "scheme-two")).pki_certificate_cn_hash_scheme == 2
    assert (await client.get(ME, headers=await _login(client, cert))).status_code == 200
    monkeypatch.setattr(settings.app, "environment", AppEnvironment.DEVELOPMENT.value)
    assert (await client.post(LOGIN, json=_dev_login(cert, "New-Name"))).status_code == 200
    wrong_case = await client.post(LOGIN, json=_dev_login(cert, "new-name"))
    assert wrong_case.status_code == 401, wrong_case.text


@pytest.mark.asyncio
async def test_a_scheme_1_row_logs_in_in_development(client, db, headers, monkeypatch):
    cert = make_certificate("Dev-Old", 0xC2)
    await _create_user(client, db, headers, "dev-old", cert)
    await _make_scheme_1(db, "dev-old", "Dev-Old")
    monkeypatch.setattr(settings.app, "environment", AppEnvironment.DEVELOPMENT.value)
    assert (await client.post(LOGIN, json=_dev_login(cert, "dev-old"))).status_code == 200


@pytest.mark.asyncio
async def test_replacing_the_certificate_moves_a_row_to_scheme_2(client, db, headers):
    cert = make_certificate("Moving", 0xC3)
    user_id = await _create_user(client, db, headers, "moving", cert)
    await _make_scheme_1(db, "moving", "Moving")
    admin = await make_admin(db)
    replacement = make_certificate("Moving", 0xC4)
    response = await client.patch(
        V1.path(UserRoutes.USER_BY_ID, user_id=user_id), headers=headers(admin),
        files={"certificate": ("cert.pem", pem(replacement), "application/x-pem-file")},
    )
    assert response.status_code == 200, response.text
    await db.rollback()
    row = await _row(db, "moving")
    await db.refresh(row)
    assert row.pki_certificate_cn_hash_scheme == 2
    assert row.pki_certificate_cn_hash == await hash_cn("Moving")


@pytest.mark.asyncio
async def test_parse_cn_picks_the_cn_the_certificate_upload_stores():
    cert = multi_cn_certificate("first-cn", "second-cn", 0xD0)
    stored = CertificateParser.parse_certificate(pem(cert))["pki_certificate_cn"]
    assert await parse_cn(openssl_rfc2253(cert.subject)) == stored
    assert await parse_cn("/C=UA/CN=" + stored + "/CN=second-cn") == stored


@pytest.mark.asyncio
async def test_a_subject_with_several_names_logs_in(client, db, headers, production):
    cert = multi_cn_certificate("first-cn", "second-cn", 0xD1)
    await _create_user(client, db, headers, "multi-cn", cert)
    me = await client.get(ME, headers=await _login(client, cert))
    assert me.status_code == 200 and me.json()["username"] == "multi-cn"


@pytest.mark.asyncio
async def test_two_names_in_one_rdn_log_in(client, db, headers, production):
    """OpenSSL reverses the attributes of a multi-valued RDN too: CN=bbb+CN=aaa for a certificate that has aaa first"""
    cert = multi_cn_certificate("aaa-first", "bbb-second", 0xD2, one_rdn=True)
    assert openssl_rfc2253(cert.subject) == "CN=bbb-second+CN=aaa-first,C=UA"
    stored = CertificateParser.parse_certificate(pem(cert))["pki_certificate_cn"]
    assert stored == "aaa-first" and await parse_cn(openssl_rfc2253(cert.subject)) == stored
    await _create_user(client, db, headers, "one-rdn", cert)
    me = await client.get(ME, headers=await _login(client, cert))
    assert me.status_code == 200 and me.json()["username"] == "one-rdn"


@pytest.mark.slow
def test_019_adds_the_column_keeps_old_rows_at_scheme_1_and_downgrades(tmp_path):
    db_file = tmp_path / "scheme.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    conn = sqlite3.connect(db_file)
    conn.execute("alter table users drop column pki_certificate_cn_hash_scheme")
    # the rest of the schema is at the head: users.user_type (023) is NOT NULL
    conn.execute("insert into users (pki_certificate_cn_hash, pki_certificate_serial_hash,"
                 " pki_certificate_fingerprint_hash, username, username_alias, is_active, is_deleted, created_at,"
                 " updated_at, user_type) values ('c', 's', 'f', 'legacy', 'L', 1, 0, '2026-01-01', '2026-01-01', 'viewer')")
    conn.execute("update alembic_version set version_num = '018_media_file_size_bigint'")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select pki_certificate_cn_hash_scheme from users").fetchone() == (1,)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()

    down = _run_downgrade(db_file, "018_media_file_size_bigint")
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert "pki_certificate_cn_hash_scheme" not in {row[1] for row in conn.execute("pragma table_info(users)")}
    assert conn.execute("select username from users").fetchone() == ("legacy",)
    conn.close()


@pytest.mark.slow
def test_a_fresh_install_makes_the_scheme_column_a_small_integer_like_019(tmp_path):
    """A fresh install builds users from the model (002_sync_schema), 019 adds the column to an existing table: both
    must give SMALLINT NOT NULL DEFAULT 1 (the model said int, so a fresh install got INTEGER)"""
    db_file = tmp_path / "fresh.db"
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_file)
    column = {row[1]: row for row in conn.execute("pragma table_info(users)")}["pki_certificate_cn_hash_scheme"]
    conn.close()
    _, _, column_type, not_null, default, _ = column
    assert (column_type, not_null, default) == ("SMALLINT", 1, "'1'")
