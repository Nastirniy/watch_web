"""Revoking a certificate: its sessions end in the same transaction, the CRL is published after the commit
(spec section 7)."""
import shutil
from datetime import datetime

import pytest
from cryptography import x509
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.config import settings
from app.backend.enums.general import CertificateSource, Permissions
from app.backend.enums.routes import SystemRoutes, UserRoutes
from app.backend.models.user import UserSession
from app.backend.pki.authority import sha256_fingerprint
from app.backend.pki.crl import split_pem_crls
from app.backend.services.crl_publisher import crl_status, publish_crl
from tests.helpers import assign, make_admin, make_certificate_row, make_post, make_role, make_user
from tests.pki_helpers import legacy_root, make_leaf, pem
from tests.test_certificate_routes import (
    _post_admin, audit_rows, issue, loaded_package, problem_code, rows_of, upload,
)
from tests.test_pki_login import _login, nginx_headers, production  # noqa: F401
from app.backend.enums.routes import AuthRoutes

LOGIN = V1.path(AuthRoutes.LOGIN)
ME = V1.path(AuthRoutes.ME)


def revoke_url(user_id: int, certificate_id: int) -> str:
    return V1.path(UserRoutes.USER_CERTIFICATE_REVOKE, user_id=user_id, certificate_id=certificate_id)


async def revoke(client, headers, caller, user_id: int, certificate_id: int, **body):
    return await client.post(revoke_url(user_id, certificate_id), json=body, headers=headers(caller))


def crl_entries(pki) -> dict:
    (crl,) = split_pem_crls(pki.crl_path.read_bytes())
    return {entry.serial_number: entry for entry in crl}


async def test_revoke_lists_the_serial_on_the_crl(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    cert = loaded_package(issued.json()).cert.certificate
    certificate_id = issued.json()["certificate"]["id"]

    r = await revoke(client, headers, admin, user.id, certificate_id, reason="key_compromise")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "revoked" and r.json()["id"] == certificate_id

    (row,) = await rows_of(db, user.id)
    assert row.revoked_at is not None and row.revoked_by == admin.id and row.revocation_reason == "key_compromise"
    entry = crl_entries(pki)[cert.serial_number]
    assert entry.extensions.get_extension_for_class(x509.CRLReason).value.reason == x509.ReasonFlags.key_compromise
    (audit,) = await audit_rows(db, "revoke")
    assert audit.resource_id == certificate_id and audit.user_id == admin.id
    assert audit.details["reason"] == "key_compromise" and audit.details["serial"] == row.serial
    # Like every certificate audit row: the fingerprint (of the stored certificate) and the expiry
    assert audit.details["sha256_fingerprint"] == sha256_fingerprint(cert)
    assert datetime.fromisoformat(audit.details["not_after"]) == cert.not_valid_after_utc
    assert (await crl_status(db)).stale is False


async def test_revoke_without_a_body_is_unspecified(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    r = await client.post(revoke_url(user.id, issued.json()["certificate"]["id"]), headers=headers(admin))
    assert r.status_code == 200, r.text
    (row,) = await rows_of(db, user.id)
    assert row.revocation_reason == "unspecified"


async def test_revoke_twice_is_409(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    certificate_id = issued.json()["certificate"]["id"]
    assert (await revoke(client, headers, admin, user.id, certificate_id)).status_code == 200
    again = await revoke(client, headers, admin, user.id, certificate_id)
    assert again.status_code == 409 and problem_code(again) == "certificate_already_revoked"
    assert len(await audit_rows(db, "revoke")) == 1


async def test_revoke_unknown_certificate_is_404(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    other = await make_user(db)
    issued = await issue(client, headers, admin, other)
    r = await revoke(client, headers, admin, user.id, issued.json()["certificate"]["id"])
    assert r.status_code == 404 and problem_code(r) == "certificate_not_found"


async def test_revoke_ends_only_that_devices_sessions(client, db, headers, pki, production):
    admin = await make_admin(db)
    user = await make_user(db)
    first = await issue(client, headers, admin, user, label="First")
    second = await issue(client, headers, admin, user, label="Second")
    first_cert = loaded_package(first.json()).cert.certificate
    second_cert = loaded_package(second.json()).cert.certificate
    first_headers = await _login(client, first_cert)
    second_headers = await _login(client, second_cert)
    assert (await client.get(ME, headers=first_headers)).status_code == 200

    r = await revoke(client, headers, admin, user.id, first.json()["certificate"]["id"])
    assert r.status_code == 200, r.text

    gone = await client.get(ME, headers=first_headers)
    # The session ended with the revocation (end_reason revoked): the ended session answers session_expired
    assert gone.status_code == 401 and problem_code(gone) == "session_expired"
    assert (await client.get(ME, headers=second_headers)).status_code == 200
    rows = (await db.exec(select(UserSession).where(UserSession.user_id == user.id)
                          .execution_options(populate_existing=True))).all()
    ended = [row for row in rows if row.ended_at is not None]
    assert len(ended) == 1 and (ended[0].end_reason, ended[0].revoke_cause) == ("revoked", "certificate_revoked")

    login = await client.post(LOGIN, headers=nginx_headers(first_cert))
    assert login.status_code == 401 and problem_code(login) == "certificate_revoked"


async def test_revoking_a_foreign_upload_blocks_login_but_not_the_crl(client, db, headers, pki, production):
    admin = await make_admin(db)
    user = await make_user(db)
    leaf, _ = make_leaf(legacy_root(), label="Foreign")
    uploaded = await upload(client, headers, admin, user, pem(leaf))
    assert uploaded.status_code == 201 and uploaded.json()["ca_issued"] is False, uploaded.text
    assert (await client.post(LOGIN, headers=nginx_headers(leaf))).status_code == 200

    r = await revoke(client, headers, admin, user.id, uploaded.json()["id"], reason="superseded")
    assert r.status_code == 200, r.text
    (audit,) = await audit_rows(db, "revoke")
    # The row keeps the expiry; the file was not stored, so its fingerprint is not known (never made up)
    assert datetime.fromisoformat(audit.details["not_after"]) == leaf.not_valid_after_utc
    assert "sha256_fingerprint" not in audit.details
    login = await client.post(LOGIN, headers=nginx_headers(leaf))
    assert login.status_code == 401 and problem_code(login) == "certificate_revoked"
    # Nothing of ours to publish: no file was written, and an explicit publish lists nothing
    assert not pki.crl_path.exists()
    assert await publish_crl(db) is True
    assert crl_entries(pki) == {}


async def test_revoking_a_legacy_row_works_without_pki(client, db, headers):
    admin = await make_admin(db)
    user = await make_user(db)
    row = await make_certificate_row(db, user, CertificateSource.LEGACY)
    r = await revoke(client, headers, admin, user.id, row.id)
    assert r.status_code == 200 and r.json()["state"] == "revoked", r.text
    (audit,) = await audit_rows(db, "revoke")
    # A legacy row has only hashes: no fingerprint, no expiry (left out, never invented)
    assert "sha256_fingerprint" not in audit.details and "not_after" not in audit.details


async def test_revocation_while_the_signer_is_down_reaches_the_next_crl(client, db, headers, pki):
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    cert = loaded_package(issued.json()).cert.certificate
    pki.signer_down()
    r = await revoke(client, headers, admin, user.id, issued.json()["certificate"]["id"])
    assert r.status_code == 200 and r.json()["state"] == "revoked", r.text
    assert (await crl_status(db)).stale is True
    assert not pki.crl_path.exists()

    pki.signer_up()
    assert await publish_crl(db) is True
    assert cert.serial_number in crl_entries(pki)
    assert (await crl_status(db)).stale is False


@pytest.mark.fit_types
async def test_revoke_permissions_and_scope(client, db, headers, pki):
    admin = await make_admin(db)
    here, there = await make_post(db), await make_post(db)
    user = await make_user(db, post_id=here.id)
    issued = await issue(client, headers, admin, user)
    certificate_id = issued.json()["certificate"]["id"]

    viewer = await make_user(db)
    await assign(db, viewer, await make_role(db, [Permissions.CERTIFICATE_VIEW, Permissions.USER_VIEW]), ROOT)
    r = await revoke(client, headers, viewer, user.id, certificate_id)
    assert r.status_code == 403 and problem_code(r) == "permission_denied"
    assert r.json()["missing"] == ["certificate.revoke"]

    outsider = await _post_admin(db, Node.post(there.id))
    r = await revoke(client, headers, outsider, user.id, certificate_id)
    assert r.status_code == 403 and problem_code(r) == "user_self_only"

    insider = await _post_admin(db, Node.post(here.id))
    r = await revoke(client, headers, insider, user.id, certificate_id)
    assert r.status_code == 200, r.text
    assert (await rows_of(db, user.id))[0].revoked_by == insider.id


@pytest.fixture
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)


async def stale_in_health(client) -> bool:
    return (await client.get(SystemRoutes.HEALTH.value)).json()["pki"]["crl_stale"]


async def test_revocation_holds_when_the_signer_refuses(client, db, headers, pki, monkeypatch, no_rate_limit):
    """The revocation is committed before the publication: a refusing signer (crl_days above SIGNER_MAX_CRL_DAYS)
    leaves the CRL stale, the answer is 200 (R7)"""
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    monkeypatch.setattr(settings.pki, "crl_days", pki.signer_settings.max_crl_days + 1)
    r = await revoke(client, headers, admin, user.id, issued.json()["certificate"]["id"])
    assert r.status_code == 200 and r.json()["state"] == "revoked", r.text
    assert not pki.crl_path.exists()
    assert await stale_in_health(client) is True


async def test_revocation_holds_with_an_unreadable_extra_crl(client, db, headers, pki, tmp_path, monkeypatch,
                                                             no_rate_limit):
    """An extra CRL file gone since the start: nothing is published (the file nginx reads stays), the answer is 200"""
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    assert await publish_crl(db) is True
    before = pki.crl_path.read_bytes()
    monkeypatch.setattr(settings.pki, "extra_crl_paths", [str(tmp_path / "legacy-root.crl")])
    r = await revoke(client, headers, admin, user.id, issued.json()["certificate"]["id"])
    assert r.status_code == 200 and r.json()["state"] == "revoked", r.text
    assert pki.crl_path.read_bytes() == before
    assert await stale_in_health(client) is True


async def test_revocation_holds_when_the_crl_cannot_be_written(client, db, headers, pki, no_rate_limit):
    admin = await make_admin(db)
    user = await make_user(db)
    issued = await issue(client, headers, admin, user)
    shutil.rmtree(pki.crl_path.parent)
    r = await revoke(client, headers, admin, user.id, issued.json()["certificate"]["id"])
    assert r.status_code == 200 and r.json()["state"] == "revoked", r.text
    assert (await rows_of(db, user.id))[0].revoked_at is not None
    assert await stale_in_health(client) is True
