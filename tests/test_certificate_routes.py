"""Certificate routes: issue, upload, read, list and re-download the certificates of a user, the list across users,
the certificate of the session in /auth/me and `pki issue` (spec sections 5, 6 and 8)."""
import base64
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.config import settings
from app.backend.enums.general import CertificateSource, Permissions, UserType
from app.backend.enums.routes import AuthRoutes, CertificateRoutes, UserRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role import Role
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.pki.authority import is_signed_by, sha256_fingerprint
from tests.helpers import assign, give, issue, make_admin, make_certificate_row, make_group, make_post, make_role, make_user
from tests.pki_helpers import legacy_root, make_leaf, pem
from tests.test_pki_login import CYRILLIC_CN, _login, nginx_headers, production  # noqa: F401

LOGIN = V1.path(AuthRoutes.LOGIN)
ME = V1.path(AuthRoutes.ME)
ALL_CERTIFICATES = V1.path(CertificateRoutes.ROOT)
VIEW_AND_CREATE = [Permissions.CERTIFICATE_VIEW, Permissions.CERTIFICATE_CREATE, Permissions.USER_VIEW]


def certificates_url(user_id: int) -> str:
    return V1.path(UserRoutes.USER_CERTIFICATES, user_id=user_id)


def upload_url(user_id: int) -> str:
    return V1.path(UserRoutes.USER_CERTIFICATE_UPLOAD, user_id=user_id)


def certificate_url(user_id: int, certificate_id: int) -> str:
    return V1.path(UserRoutes.USER_CERTIFICATE_BY_ID, user_id=user_id, certificate_id=certificate_id)


def export_url(user_id: int, certificate_id: int) -> str:
    return V1.path(UserRoutes.USER_CERTIFICATE_EXPORT, user_id=user_id, certificate_id=certificate_id)


async def upload(client, headers, caller, user, data: bytes, label: str = "Office PC"):
    return await client.post(upload_url(user.id), headers=headers(caller),
                             files={"file": ("cert.pem", data, "application/x-pem-file")}, data={"label": label})


def loaded_package(body: dict):
    return pkcs12.load_pkcs12(base64.b64decode(body["p12_base64"]), body["password"].encode())


async def rows_of(db, user_id: int) -> list:
    """The certificate rows of the user as stored now (the routes write them through their own sessions)"""
    result = await db.exec(select(UserCertificate).where(UserCertificate.user_id == user_id)
                           .order_by(UserCertificate.id).execution_options(populate_existing=True))
    return result.all()


async def audit_rows(db, action: str) -> list:
    result = await db.exec(select(AuditLog).where(AuditLog.resource_type == "user_certificate",
                                                  AuditLog.action == action).order_by(AuditLog.id))
    return result.all()


async def builtin_role(db, user_type: UserType) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == user_type.value))).one()


async def archive(db, user: User) -> None:
    user.is_deleted = True
    db.add(user)
    await db.commit()


def problem_code(response) -> str:
    return response.json()["code"]


def field_error(response) -> dict:
    assert response.status_code == 422, response.text
    (error,) = response.json()["errors"]
    return error


# ------------------------------------------------------------------ issue

async def test_issue_returns_the_package_once(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    r = await issue(client, headers, admin, user)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store", r.text
    body = r.json()
    loaded = loaded_package(body)
    assert loaded.cert.friendly_name == b"Laptop" and is_signed_by(loaded.cert.certificate, pki.ca[0])
    assert loaded.additional_certs == []
    assert body["filename"] == "Laptop.p12" and body["certificate"]["state"] == "active" and body["certificate"]["has_package"]
    folder = pki.store_dir / f"{user.id}" / f"{body['certificate']['id']}"
    assert (folder / "certificate.p12").exists()
    assert (folder / "password.txt").read_text(encoding="utf-8") == body["password"]
    assert (folder / "certificate.p12").read_bytes() == base64.b64decode(body["p12_base64"])
    info = (folder / "info.txt").read_text(encoding="utf-8")
    assert "Laptop" in info and user.username in info and body["password"] not in info

    record = body["certificate"]
    cert = loaded.cert.certificate
    assert record["source"] == "issued" and record["label"] == "Laptop" and record["subject_cn"] == "Laptop"
    assert record["serial"] == format(cert.serial_number, "x") and record["ca_issued"] is True
    assert record["user_id"] == user.id and record["created_by"] == admin.id and record["export_count"] == 0
    assert not {"fingerprint_hash", "cn_hash", "cn_hash_scheme", "serial_hash", "files_dir"} & set(record)
    validity = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert validity == timedelta(days=settings.pki.validity_days, minutes=5)

    (row,) = await rows_of(db, user.id)
    assert row.source == CertificateSource.ISSUED.value and row.files_dir == f"{user.id}/{row.id}" and row.ca_issued

    (audit,) = await audit_rows(db, "issue")
    assert audit.resource_id == row.id and audit.user_id == admin.id
    assert audit.params["label"] == "Laptop" and audit.params["serial"] == row.serial
    assert audit.params["sha256_fingerprint"] == sha256_fingerprint(cert) and audit.params["not_after"]
    stored = json.dumps([audit.params, audit.new_values, audit.old_values])
    assert body["password"] not in stored and body["p12_base64"][:40] not in stored and "PRIVATE KEY" not in stored


async def test_issue_takes_the_validity_asked_for(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    r = await issue(client, headers, admin, user, validity_days=30)
    assert r.status_code == 201, r.text
    cert = loaded_package(r.json()).cert.certificate
    assert cert.not_valid_after_utc - cert.not_valid_before_utc == timedelta(days=30, minutes=5)


async def test_issued_certificate_logs_in_through_nginx(client, db, headers, pki, production):
    admin = await make_admin(db)
    user = await make_user(db)
    r = await issue(client, headers, admin, user)
    assert r.status_code == 201, r.text
    me = await client.get(ME, headers=await _login(client, loaded_package(r.json()).cert.certificate))
    assert me.status_code == 200 and me.json()["username"] == user.username


async def test_issued_certificate_logs_in_through_the_development_json(client, db, headers, pki):
    # ASCII label: the CN pattern of the JSON login is ASCII only
    admin = await make_admin(db)
    user = await make_user(db)
    r = await issue(client, headers, admin, user, label="Dev laptop")
    assert r.status_code == 201, r.text
    cert = loaded_package(r.json()).cert.certificate
    login = await client.post(LOGIN, json={
        "certificate_cn": "Dev laptop",
        "certificate_serial": format(cert.serial_number, "x"),
        "certificate_fingerprint": hashlib.sha1(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
    })
    assert login.status_code == 200, login.text
    assert login.json()["user_id"] == user.id


async def test_cyrillic_label_logs_in_through_nginx(client, db, headers, pki, production):
    admin = await make_admin(db)
    user = await make_user(db)
    r = await issue(client, headers, admin, user, label=CYRILLIC_CN)
    assert r.status_code == 201, r.text
    body = r.json()
    loaded = loaded_package(body)
    assert loaded.cert.friendly_name == CYRILLIC_CN.encode("utf-8")
    assert body["certificate"]["label"] == CYRILLIC_CN
    assert body["filename"] == f"certificate-{body['certificate']['id']}.p12"
    me = await client.get(ME, headers=await _login(client, loaded.cert.certificate))
    assert me.status_code == 200 and me.json()["username"] == user.username


async def test_failed_issue_leaves_no_directory(client, db, headers, pki, monkeypatch):
    admin = await make_admin(db)
    user = await make_user(db)

    async def failing_audit(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.backend.api.v1.user_certificates.create_audit_log", failing_audit)
    r = await issue(client, headers, admin, user)
    assert r.status_code == 500 and problem_code(r) == "internal_error"
    assert await rows_of(db, user.id) == []
    user_dir = pki.store_dir / str(user.id)
    assert user_dir.is_dir() and list(user_dir.iterdir()) == []


async def test_issue_without_pki_or_signer(client, db, headers, pki, monkeypatch):
    admin = await make_admin(db)
    user = await make_user(db)
    with monkeypatch.context() as patch:
        patch.setattr(settings.pki, "signer_socket", "")
        r = await issue(client, headers, admin, user)
        assert r.status_code == 503 and problem_code(r) == "certificate_authority_not_configured"

    pki.signer_down()
    r = await issue(client, headers, admin, user)
    assert r.status_code == 503 and problem_code(r) == "certificate_authority_unavailable"
    assert await rows_of(db, user.id) == []
    assert not (pki.store_dir / str(user.id)).exists()


async def test_issue_to_an_archived_user_is_404(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    await archive(db, user)
    r = await issue(client, headers, admin, user)
    assert r.status_code == 404 and problem_code(r) == "user_not_found"
    r = await upload(client, headers, admin, user, pem(make_leaf(pki.ca)[0]))
    assert r.status_code == 404 and problem_code(r) == "user_not_found"
    assert await rows_of(db, user.id) == []


async def test_issue_to_an_unknown_user_is_404(client, db, headers, pki):
    admin = await make_admin(db)
    r = await client.post(certificates_url(999999), json={"label": "Laptop"}, headers=headers(admin))
    assert r.status_code == 404 and problem_code(r) == "user_not_found"


async def test_label_and_validity_errors(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    for label in ("", "   ", "x" * 65, "bell\x07"):
        error = field_error(await issue(client, headers, admin, user, label=label))
        assert error["code"] == "certificate_label_invalid", label
    error = field_error(await issue(client, headers, admin, user, validity_days=731))
    assert error["code"] == "validity_days_too_long" and error["max_days"] == 730
    error = field_error(await issue(client, headers, admin, user, validity_days=0))
    assert error["code"] == "greater_than_equal"
    error = field_error(await upload(client, headers, admin, user, pem(make_leaf(pki.ca)[0]), label=""))
    assert error["code"] == "certificate_label_invalid"
    assert (await issue(client, headers, admin, user, label="x" * 64, validity_days=730)).status_code == 201
    assert await rows_of(db, user.id) != []


# ------------------------------------------------------------------ upload

async def test_upload_route(client, db, headers, pki, monkeypatch):
    admin = await make_admin(db)
    user = await make_user(db)
    ours, _ = make_leaf(pki.ca, label="Our CN")
    r = await upload(client, headers, admin, user, pem(ours))
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["source"] == "uploaded" and record["ca_issued"] is True and record["label"] == "Office PC"
    assert record["subject_cn"] == "Our CN" and record["state"] == "active" and record["has_package"] is False
    assert record["created_by"] == admin.id

    foreign, _ = make_leaf(legacy_root(), label="Foreign CN")
    r = await upload(client, headers, admin, user, foreign.public_bytes(serialization.Encoding.DER), label="DER")
    assert r.status_code == 201 and r.json()["ca_issued"] is False

    r = await upload(client, headers, admin, user, pem(ours))
    assert r.status_code == 400 and problem_code(r) == "certificate_taken"

    expired, _ = make_leaf(pki.ca, days=1, now=datetime.now(timezone.utc) - timedelta(days=10))
    r = await upload(client, headers, admin, user, pem(expired))
    assert r.status_code == 201 and r.json()["state"] == "expired"

    r = await upload(client, headers, admin, user, b"")
    assert r.status_code == 400 and problem_code(r) == "certificate_file_empty"
    r = await upload(client, headers, admin, user, b"not a certificate")
    assert r.status_code == 400 and problem_code(r) == "certificate_invalid"
    monkeypatch.setattr("app.backend.api.v1.user_certificates.MAX_CERTIFICATE_BYTES", 100)
    r = await upload(client, headers, admin, user, pem(make_leaf(pki.ca)[0]))
    assert r.status_code == 413 and problem_code(r) == "certificate_file_too_large" and r.json()["max_bytes"] == 100

    assert len(await rows_of(db, user.id)) == 3
    uploads = await audit_rows(db, "upload")
    assert len(uploads) == 3 and uploads[0].params["sha256_fingerprint"] == sha256_fingerprint(ours)
    assert uploads[0].params["label"] == "Office PC" and uploads[0].params["serial"] == format(ours.serial_number, "x")


async def test_upload_works_without_pki(client, db, headers):
    admin = await make_admin(db)
    user = await make_user(db)
    cert, _ = make_leaf(legacy_root(), label="Plain")
    r = await upload(client, headers, admin, user, pem(cert))
    assert r.status_code == 201 and r.json()["ca_issued"] is False


# ------------------------------------------------------------------ export

async def test_export_counts_and_audits(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    issued = (await issue(client, headers, admin, user)).json()
    certificate_id = issued["certificate"]["id"]
    r = await client.post(export_url(user.id, certificate_id), headers=headers(admin))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store", r.text
    body = r.json()
    assert body["p12_base64"] == issued["p12_base64"] and body["password"] == issued["password"]
    assert body["filename"] == "Laptop.p12"
    assert body["certificate"]["export_count"] == 1 and body["certificate"]["last_exported_at"]

    r = await client.post(export_url(user.id, certificate_id), headers=headers(admin))
    assert r.json()["certificate"]["export_count"] == 2
    (row,) = await rows_of(db, user.id)
    assert row.export_count == 2 and row.last_exported_at is not None
    exports = await audit_rows(db, "export")
    assert len(exports) == 2 and exports[0].resource_id == certificate_id and exports[0].user_id == admin.id
    assert exports[0].params["sha256_fingerprint"] == sha256_fingerprint(loaded_package(body).cert.certificate)
    assert issued["password"] not in json.dumps([exports[0].params, exports[0].new_values])


async def test_export_refusals(client, db, headers, pki, monkeypatch):
    admin = await make_admin(db)
    user = await make_user(db)
    uploaded = (await upload(client, headers, admin, user, pem(make_leaf(pki.ca)[0]))).json()
    r = await client.post(export_url(user.id, uploaded["id"]), headers=headers(admin))
    assert r.status_code == 409 and problem_code(r) == "certificate_not_exportable"

    revoked_id = (await issue(client, headers, admin, user, label="Revoked")).json()["certificate"]["id"]
    row = next(row for row in await rows_of(db, user.id) if row.id == revoked_id)
    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    await db.commit()
    r = await client.post(export_url(user.id, revoked_id), headers=headers(admin))
    assert r.status_code == 409 and problem_code(r) == "certificate_not_active"

    gone_id = (await issue(client, headers, admin, user, label="Gone")).json()["certificate"]["id"]
    for path in (pki.store_dir / str(user.id) / str(gone_id)).iterdir():
        path.unlink()
    (pki.store_dir / str(user.id) / str(gone_id)).rmdir()
    r = await client.post(export_url(user.id, gone_id), headers=headers(admin))
    assert r.status_code == 410 and problem_code(r) == "certificate_files_missing"

    monkeypatch.setattr(settings.pki, "store_dir", "")
    r = await client.post(export_url(user.id, revoked_id), headers=headers(admin))
    assert r.status_code == 503 and problem_code(r) == "certificate_authority_not_configured"
    assert await audit_rows(db, "export") == []


# ------------------------------------------------------------------ who may do what

async def _post_admin(db, node: Node) -> User:
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await builtin_role(db, UserType.ADMINISTRATOR), node)
    return user


async def test_post_admin_cannot_export(client, db, headers, pki):
    post = await make_post(db)
    post_admin = await _post_admin(db, Node.post(post.id))
    user = await make_user(db, post_id=post.id)
    issued = await issue(client, headers, post_admin, user)
    assert issued.status_code == 201, issued.text
    r = await client.post(export_url(user.id, issued.json()["certificate"]["id"]), headers=headers(post_admin))
    assert r.status_code == 403 and problem_code(r) == "permission_denied"
    assert r.json()["missing"] == ["certificate.export"]


@pytest.mark.fit_types
async def test_delegation_inside_and_outside_scope(client, db, headers, pki):
    east, west = await make_group(db, name="East"), await make_group(db, name="West")
    east_post, west_post = await make_post(db, group_id=east.id), await make_post(db, group_id=west.id)
    post_admin = await _post_admin(db, Node.group(east.id))

    inside = await make_user(db, post_id=east_post.id)
    r = await issue(client, headers, post_admin, inside)
    assert r.status_code == 201, r.text
    cert, _ = make_leaf(pki.ca, label="Uploaded inside")
    assert (await upload(client, headers, post_admin, inside, pem(cert))).status_code == 201
    listed = await client.get(certificates_url(inside.id), headers=headers(post_admin))
    assert listed.status_code == 200 and len(listed.json()) == 2

    outside = await make_user(db, post_id=west_post.id)
    r = await issue(client, headers, post_admin, outside)
    assert r.status_code == 403 and problem_code(r) == "user_self_only"
    r = await client.get(certificates_url(outside.id), headers=headers(post_admin))
    assert r.status_code == 403 and problem_code(r) == "user_self_only"

    stronger = await make_user(db, post_id=east_post.id)
    await assign(db, stronger, await make_role(db, [Permissions.AUDIT_VIEW]), ROOT)
    r = await issue(client, headers, post_admin, stronger)
    assert r.status_code == 403 and problem_code(r) == "target_user_more_privileged"
    assert r.json()["missing"] == ["audit.view"]
    r = await upload(client, headers, post_admin, stronger, pem(make_leaf(pki.ca)[0]))
    assert r.status_code == 403 and problem_code(r) == "target_user_more_privileged"
    assert await rows_of(db, outside.id) == [] and await rows_of(db, stronger.id) == []


@pytest.mark.fit_types
async def test_user_view_without_certificate_view(client, db, headers, pki):
    """certificate.view must cover the user, not just be held somewhere"""
    here, there = await make_post(db), await make_post(db)
    caller = await make_user(db, post_id=here.id)
    await assign(db, caller, await make_role(db, [Permissions.USER_VIEW]), Node.post(here.id))
    await assign(db, caller, await make_role(db, [Permissions.CERTIFICATE_VIEW]), Node.post(there.id))
    user = await make_user(db, post_id=here.id)
    row = await make_certificate_row(db, user)
    for url in (certificates_url(user.id), certificate_url(user.id, row.id)):
        r = await client.get(url, headers=headers(caller))
        assert r.status_code == 403 and problem_code(r) == "permission_denied", url
        assert r.json()["missing"] == ["certificate.view"]


async def test_own_certificates_need_certificate_view(client, db, headers, pki):
    user = await make_user(db)
    await make_certificate_row(db, user)
    r = await client.get(certificates_url(user.id), headers=headers(user))
    assert r.status_code == 403 and problem_code(r) == "permission_denied"


@pytest.mark.fit_types
async def test_check_order(client, db, headers, pki):
    """Scope first: an outsider learns nothing about the user's certificates, not even whether one exists"""
    here, there = await make_post(db), await make_post(db)
    outsider = await make_user(db, post_id=there.id)
    await give(db, outsider, VIEW_AND_CREATE + [Permissions.CERTIFICATE_EXPORT])
    restricted = await make_user(db, post_id=there.id)
    await assign(db, restricted, await make_role(db, VIEW_AND_CREATE), Node.post(there.id))
    foreign = await make_user(db, post_id=here.id)
    for url in (certificate_url(foreign.id, 999999), certificates_url(foreign.id)):
        r = await client.get(url, headers=headers(restricted))
        assert r.status_code == 403 and problem_code(r) == "user_self_only", url
    r = await client.post(export_url(foreign.id, 999999), headers=headers(restricted))
    assert r.status_code == 403
    r = await client.get(certificate_url(foreign.id, 999999), headers=headers(outsider))
    assert r.status_code == 404 and problem_code(r) == "certificate_not_found"

    gone = await make_user(db, post_id=here.id)
    await archive(db, gone)
    r = await client.get(certificates_url(gone.id), headers=headers(restricted))
    assert r.status_code == 403 and problem_code(r) == "user_self_only"


@pytest.mark.fit_types
async def test_archived_user_needs_user_archive(client, db, headers, pki):
    post = await make_post(db)
    viewer = await make_user(db, post_id=post.id)
    await assign(db, viewer, await make_role(db, [Permissions.CERTIFICATE_VIEW, Permissions.USER_VIEW]), Node.post(post.id))
    admin = await make_admin(db)
    user = await make_user(db, post_id=post.id)
    row = await make_certificate_row(db, user)
    await archive(db, user)
    r = await client.get(certificates_url(user.id), headers=headers(viewer))
    assert r.status_code == 404 and problem_code(r) == "user_not_found"
    r = await client.get(certificate_url(user.id, row.id), headers=headers(admin))
    assert r.status_code == 200 and r.json()["id"] == row.id


async def test_another_users_certificate_is_404(client, db, headers, pki):
    admin = await make_admin(db)
    owner, other = await make_user(db), await make_user(db)
    issued = (await issue(client, headers, admin, owner)).json()["certificate"]
    r = await client.get(certificate_url(owner.id, issued["id"]), headers=headers(admin))
    assert r.status_code == 200 and r.json() == issued
    for response in (await client.get(certificate_url(other.id, issued["id"]), headers=headers(admin)),
                     await client.post(export_url(other.id, issued["id"]), headers=headers(admin))):
        assert response.status_code == 404 and problem_code(response) == "certificate_not_found"


# ------------------------------------------------------------------ lists

@pytest.mark.fit_types
async def test_list_and_filters(client, db, headers, pki):
    now = datetime.now(timezone.utc)
    p1, p2 = await make_post(db), await make_post(db)
    u1, u2 = await make_user(db, post_id=p1.id), await make_user(db, post_id=p2.id)
    homeless = await make_user(db)
    gone = await make_user(db, post_id=p1.id)
    soon = (await make_certificate_row(db, u1, source=CertificateSource.ISSUED, not_after=now + timedelta(days=5))).id
    later = (await make_certificate_row(db, u1, not_after=now + timedelta(days=100))).id
    revoked = (await make_certificate_row(db, u2, revoked_at=now, not_after=now + timedelta(days=5))).id
    expired = (await make_certificate_row(db, u2, not_after=now - timedelta(days=1))).id
    floating = (await make_certificate_row(db, homeless, source=CertificateSource.LEGACY)).id
    await make_certificate_row(db, gone)
    await archive(db, gone)
    admin = await make_admin(db)

    async def per_user(user, **params):
        r = await client.get(certificates_url(user.id), params=params, headers=headers(admin))
        assert r.status_code == 200, r.text
        return [record["id"] for record in r.json()]

    assert await per_user(u2) == [revoked, expired]
    assert await per_user(u2, state="revoked") == [revoked]
    assert await per_user(u2, state="expired") == [expired]
    assert await per_user(u1, state="active") == [soon, later]

    async def listed(caller=admin, **params):
        r = await client.get(ALL_CERTIFICATES, params=params, headers=headers(caller))
        assert r.status_code == 200, r.text
        return [record["id"] for record in r.json()]

    assert await listed() == [soon, later, revoked, expired, floating]
    assert await listed(state="active") == [soon, later, floating]
    assert await listed(state="revoked") == [revoked]
    assert await listed(state="expired") == [expired]
    assert await listed(source="issued") == [soon]
    assert await listed(source="legacy") == [floating]
    assert await listed(user_id=u2.id) == [revoked, expired]
    assert await listed(post_id=p1.id) == [soon, later]
    assert await listed(expires_within_days=10) == [soon, revoked]
    assert await listed(expires_within_days=10, state="active") == [soon]
    assert await listed(sort_by="not_after", sort_order="descending", source="uploaded") == [later, revoked, expired]
    assert await listed(page_size=2, page=2) == [revoked, expired]

    records = (await client.get(ALL_CERTIFICATES, params={"user_id": u2.id}, headers=headers(admin))).json()
    assert [record["state"] for record in records] == ["revoked", "expired"]

    # Both certificate.view and user.view where the user's home post is; users without one only from the root
    viewer = await make_user(db, post_id=p1.id)
    await assign(db, viewer, await make_role(db, [Permissions.CERTIFICATE_VIEW, Permissions.USER_VIEW]),
                 Node.post(p1.id))
    await assign(db, viewer, await make_role(db, [Permissions.CERTIFICATE_VIEW]), Node.post(p2.id))
    assert await listed(viewer) == [soon, later]
    assert await listed(viewer, post_id=p1.id) == [soon, later]
    r = await client.get(ALL_CERTIFICATES, params={"post_id": p2.id}, headers=headers(viewer))
    assert r.status_code == 403 and problem_code(r) == "post_access_denied"


async def test_list_validation(client, db, headers):
    admin = await make_admin(db)
    for params in ({"sort_by": "fingerprint_hash"}, {"state": "lost"}, {"expires_within_days": -1},
                   {"expires_within_days": 3651}):
        r = await client.get(ALL_CERTIFICATES, params=params, headers=headers(admin))
        assert r.status_code == 422, params


# ------------------------------------------------------------------ /auth/me

async def test_me_shows_the_session_certificate(client, db, headers, pki, production):
    admin = await make_admin(db)
    user = await make_user(db)
    body = (await issue(client, headers, admin, user)).json()
    me = await client.get(ME, headers=await _login(client, loaded_package(body).cert.certificate))
    assert me.status_code == 200, me.text
    certificate = me.json()["certificate"]
    assert certificate["id"] == body["certificate"]["id"] and certificate["label"] == "Laptop"
    assert certificate["state"] == "active" and certificate["not_after"] == body["certificate"]["not_after"]

    # A session bound to no certificate (development login)
    me = await client.get(ME, headers=headers(user))
    assert me.status_code == 200 and me.json()["certificate"] is None


# ------------------------------------------------------------------ files and CLI

@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_issued_files_permissions(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    body = (await issue(client, headers, admin, user)).json()
    folder = pki.store_dir / str(user.id) / str(body["certificate"]["id"])
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "certificate.p12").stat().st_mode) == 0o600
    assert stat.S_IMODE((folder / "password.txt").stat().st_mode) == 0o600


async def test_cli_issue(session_maker, db, pki, tmp_path, monkeypatch):
    from app.backend.cli import pki as pki_cli

    async def test_session_maker():
        return session_maker

    monkeypatch.setattr(pki_cli, "get_session_maker", test_session_maker)
    closed = []

    async def close_db():
        closed.append(True)

    monkeypatch.setattr(pki_cli, "close_db", close_db)
    user = await make_user(db)
    out = tmp_path / "out"
    assert await pki_cli.run(["issue", "--username", user.username, "--label", "Laptop", "--days", "30",
                              "--out", str(out)]) == 0

    (row,) = await rows_of(db, user.id)
    assert row.created_by is None and row.source == CertificateSource.ISSUED.value and row.label == "Laptop"
    assert sorted(path.name for path in out.iterdir()) == ["Laptop.p12", "password.txt"]
    password = (out / "password.txt").read_text(encoding="utf-8").strip()
    loaded = pkcs12.load_pkcs12((out / "Laptop.p12").read_bytes(), password.encode())
    assert loaded.cert.certificate.not_valid_after_utc - loaded.cert.certificate.not_valid_before_utc == \
        timedelta(days=30, minutes=5)
    (audit,) = await audit_rows(db, "issue")
    assert audit.params["via"] == "cli" and audit.user_id is None and audit.resource_id == row.id

    # The files of a former run are never overwritten; unknown users and bad labels issue nothing
    assert await pki_cli.run(["issue", "--username", user.username, "--label", "Laptop", "--out", str(out)]) == 1
    assert await pki_cli.run(["issue", "--username", "nobody", "--label", "Laptop",
                              "--out", str(tmp_path / "other")]) == 1
    assert await pki_cli.run(["issue", "--username", user.username, "--label", "x" * 65,
                              "--out", str(tmp_path / "other")]) == 1
    assert await pki_cli.run(["issue", "--username", user.username, "--label", "Laptop", "--days", "731",
                              "--out", str(tmp_path / "other")]) == 1
    assert len(await rows_of(db, user.id)) == 1
    # Every run closes the pool while the event loop still runs (no aiomysql "Event loop is closed" at exit)
    assert len(closed) == 5
