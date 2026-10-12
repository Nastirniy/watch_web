"""Publishing the CRL: content, number, freshness, a signer that does not answer, the status"""
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlmodel import select

from app.backend.config import settings
from app.backend.enums.general import CertificateSource, RevocationReason
from app.backend.models.user_certificate import CertificateAuthorityState
from app.backend.pki.crl import build_crl, split_pem_crls
from app.backend.services.crl_publisher import crl_status, publish_crl
from tests.helpers import make_certificate_row, make_user
from tests.pki_helpers import legacy_root, make_leaf, pem


async def revoked_row(db, user, pki, source=CertificateSource.ISSUED, ca_issued=True, days=30, revoked_at=None):
    cert, _ = make_leaf(pki.ca, days=days)
    row = await make_certificate_row(
        db, user, source, cert, ca_issued=ca_issued,
        revoked_at=revoked_at or datetime.now(timezone.utc) - timedelta(hours=1),
        revocation_reason=RevocationReason.KEY_COMPROMISE.value)
    return cert, row


async def state_of(db) -> CertificateAuthorityState:
    db.expire_all()
    return (await db.exec(select(CertificateAuthorityState))).one()


async def test_publish_writes_a_signed_crl_with_the_extra_crls(db, pki, tmp_path, monkeypatch):
    user = await make_user(db)
    cert, _ = await revoked_row(db, user, pki)
    await revoked_row(db, user, pki, source=CertificateSource.UPLOADED, ca_issued=False)
    expired_cert, _ = make_leaf(pki.ca, days=1, now=datetime.now(timezone.utc) - timedelta(days=10))
    await make_certificate_row(db, user, CertificateSource.ISSUED, expired_cert, ca_issued=True,
                               revoked_at=datetime.now(timezone.utc) - timedelta(days=5),
                               revocation_reason=RevocationReason.SUPERSEDED.value)
    root = legacy_root()
    now = datetime.now(timezone.utc)
    extra = pem(build_crl(root[0], root[1], 5, now, now + timedelta(days=30), []))
    extra_path = tmp_path / "legacy-root.crl"
    extra_path.write_bytes(extra)
    monkeypatch.setattr(settings.pki, "extra_crl_paths", [str(extra_path)])

    assert await publish_crl(db) is True

    crls = split_pem_crls(pki.crl_path.read_bytes())
    assert len(crls) == 2
    first = crls[0]
    assert [entry.serial_number for entry in first] == [cert.serial_number]
    assert first.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 1
    assert first.is_signature_valid(pki.ca[0].public_key())
    assert crls[1].public_bytes(serialization.Encoding.PEM).strip() == extra.strip()
    state = await state_of(db)
    assert (state.crl_number, state.revoked_count) == (1, 1)
    assert state.published_at is not None and state.next_update is not None


async def test_number_grows(db, pki):
    assert await publish_crl(db) is True
    assert await publish_crl(db) is True
    crl = split_pem_crls(pki.crl_path.read_bytes())[0]
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 2
    assert (await state_of(db)).crl_number == 2


async def test_only_if_due_skips_a_fresh_crl(db, pki):
    assert await publish_crl(db) is True
    before = pki.crl_path.read_bytes()
    assert await publish_crl(db, only_if_due=True) is False
    assert pki.crl_path.read_bytes() == before
    pki.crl_path.unlink()
    assert await publish_crl(db, only_if_due=True) is True


async def test_only_if_due_publishes_past_half_of_the_validity(db, pki):
    assert await publish_crl(db) is True
    state = await state_of(db)
    state.this_update = datetime.now(timezone.utc) - timedelta(days=5)
    state.next_update = state.this_update + timedelta(days=7)
    db.add(state)
    await db.commit()
    assert await publish_crl(db, only_if_due=True) is True


async def test_signer_down_keeps_the_number(db, pki):
    pki.signer_down()
    assert await publish_crl(db) is False
    assert not pki.crl_path.exists()
    db.expire_all()
    state = (await db.exec(select(CertificateAuthorityState))).first()
    assert state is None or (state.crl_number, state.published_at) == (0, None)


async def test_not_configured_publishes_nothing(db, monkeypatch):
    monkeypatch.setattr(settings.pki, "crl_path", "")
    assert await publish_crl(db) is False


async def test_status_is_stale_after_an_unpublished_revocation(db, pki):
    user = await make_user(db)
    status = await crl_status(db)
    assert (status.stale, status.this_update, status.earliest_next_update) == (False, None, None)
    await publish_crl(db)
    status = await crl_status(db)
    assert (status.stale, status.revoked_count) == (False, 0)
    assert status.earliest_next_update is not None
    await revoked_row(db, user, pki, revoked_at=datetime.now(timezone.utc))
    assert (await crl_status(db)).stale is True
    await publish_crl(db)
    assert (await crl_status(db)).stale is False


async def test_status_is_stale_when_revoked_rows_were_never_published(db, pki):
    user = await make_user(db)
    await revoked_row(db, user, pki)
    assert (await crl_status(db)).stale is True
