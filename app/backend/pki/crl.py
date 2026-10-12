"""Certificate revocation lists"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa

from app.backend.enums.general import RevocationReason

_REASONS = {
    RevocationReason.KEY_COMPROMISE: x509.ReasonFlags.key_compromise,
    RevocationReason.AFFILIATION_CHANGED: x509.ReasonFlags.affiliation_changed,
    RevocationReason.SUPERSEDED: x509.ReasonFlags.superseded,
    RevocationReason.CESSATION_OF_OPERATION: x509.ReasonFlags.cessation_of_operation,
}
_CRL_BLOCK = re.compile(rb"-----BEGIN X509 CRL-----.*?-----END X509 CRL-----", re.DOTALL)


@dataclass(frozen=True)
class CrlEntry:
    serial: int
    revoked_at: datetime
    reason: RevocationReason


def build_crl(
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    number: int,
    this_update: datetime,
    next_update: datetime,
    entries: Sequence[CrlEntry],
) -> x509.CertificateRevocationList:
    """A CRL signed with SHA-256; CRLReason is written only for a reason other than unspecified"""
    try:
        ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    except x509.ExtensionNotFound:  # an authority made without extensions (the legacy root)
        ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_cert.public_key())
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(this_update)
        .next_update(next_update)
        .add_extension(x509.CRLNumber(number), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
    )
    for entry in entries:
        revoked = x509.RevokedCertificateBuilder().serial_number(entry.serial).revocation_date(entry.revoked_at)
        flag = _REASONS.get(entry.reason)
        if flag is not None:
            revoked = revoked.add_extension(x509.CRLReason(flag), critical=False)
        builder = builder.add_revoked_certificate(revoked.build())
    return builder.sign(ca_key, hashes.SHA256())


def split_pem_crls(data: bytes) -> list[x509.CertificateRevocationList]:
    """Every CRL of a PEM bundle (cryptography has no bulk loader)"""
    return [x509.load_pem_x509_crl(block) for block in _CRL_BLOCK.findall(data)]


def earliest_next_update(data: bytes) -> Optional[datetime]:
    """The soonest next_update of a PEM bundle, None for a bundle without CRLs"""
    updates = [crl.next_update_utc for crl in split_pem_crls(data) if crl.next_update_utc is not None]
    return min(updates) if updates else None
