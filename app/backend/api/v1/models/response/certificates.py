import base64
from datetime import datetime
from typing import Optional

from pydantic import Field

from app.backend.api.shared.openapi_docs import header
from app.backend.enums.general import CertificateSource, CertificateState, RevocationReason
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.certificates import CertificatePackage, certificate_state
from app.backend.utils.validators import PlainTextModel, ResponseDatetime

# Headers of a response carrying a package (the key and its password): never stored by a cache
PACKAGE_HEADERS = {"Cache-Control": header("no-store: the package and its password are never cached")}


class CertificateResponse(PlainTextModel):
    """A client certificate of a user: every column but the hashes and the folder, plus its state"""
    id: int
    user_id: int
    source: CertificateSource = Field(description="issued (by this server), uploaded (a file), legacy (from before "
                                                  "the upgrade, hashes only) or configured (the development seed)")
    label: str = Field(description="Readable name (CN and .p12 friendly name); empty for legacy and configured")
    subject_cn: Optional[str] = Field(description="CN of the certificate; null for legacy and configured")
    serial: Optional[str] = Field(description="Serial number, lower-case hex; null for legacy and configured")
    not_before: Optional[ResponseDatetime] = None
    not_after: Optional[ResponseDatetime] = Field(
        default=None, description="When it expires; null for legacy and configured (active until revoked)")
    ca_issued: bool = Field(description="Signed by the client CA of this server: listed on its CRL once revoked")
    state: CertificateState = Field(description="Computed when read: revoked when revoked_at is set, else expired "
                                                "when not_after has passed, else active")
    has_package: bool = Field(description="Issued here with a stored package: certificate.export can download it "
                                          "again while it is active")
    revoked_at: Optional[ResponseDatetime] = None
    revoked_by: Optional[int] = None
    revocation_reason: Optional[RevocationReason] = None
    export_count: int = Field(description="How often the stored package was downloaded again")
    last_exported_at: Optional[ResponseDatetime] = None
    created_at: ResponseDatetime
    created_by: Optional[int] = Field(description="Who registered it; null for the migration, the seed and the CLI")


def certificate_to_response(row: UserCertificate, now: Optional[datetime] = None) -> CertificateResponse:
    return CertificateResponse(
        id=row.id,
        user_id=row.user_id,
        source=row.source,
        label=row.label,
        subject_cn=row.subject_cn,
        serial=row.serial,
        not_before=row.not_before,
        not_after=row.not_after,
        ca_issued=row.ca_issued,
        state=certificate_state(row, now),
        has_package=row.source == CertificateSource.ISSUED.value and bool(row.files_dir),
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revocation_reason=row.revocation_reason,
        export_count=row.export_count,
        last_exported_at=row.last_exported_at,
        created_at=row.created_at,
        created_by=row.created_by,
    )


class CertificatePackageResponse(PlainTextModel):
    """An issued certificate with its package (answered with Cache-Control: no-store)"""
    certificate: CertificateResponse
    p12_base64: str = Field(description="The .p12 file (key and certificate under the label as friendly name, no CA "
                                        "certificate), base64")
    password: str = Field(description="Password of the .p12: 20 characters in four groups of 5 joined by '-'")
    filename: str = Field(description="Suggested file name: the label's ASCII letters and digits, other characters "
                                      "as '-' (certificate-<id>.p12 when none are left)")


def package_to_response(package: CertificatePackage) -> CertificatePackageResponse:
    return CertificatePackageResponse(
        certificate=certificate_to_response(package.certificate),
        p12_base64=base64.b64encode(package.p12).decode("ascii"),
        password=package.password,
        filename=package.filename,
    )


class SessionCertificate(PlainTextModel):
    """The certificate the current session was opened with (GET /auth/me)"""
    id: int
    label: str
    not_after: Optional[ResponseDatetime] = Field(description="When it expires; null for legacy and configured")
    state: CertificateState


class CrlInfo(PlainTextModel):
    this_update: Optional[ResponseDatetime] = Field(description="When the published CRL was made; null before the first")
    next_update: Optional[ResponseDatetime] = Field(description="When the published CRL expires; null before the first")
    revoked_count: int = Field(description="Certificates on the published CRL")


class CertificateAuthorityResponse(PlainTextModel):
    subject: str = Field(description="Subject of the client CA, RFC 4514")
    not_after: ResponseDatetime = Field(description="When the client CA certificate expires")
    fingerprint_sha256: str = Field(description="SHA-256 of the client CA certificate, hex pairs joined by ':'")
    certificate_pem: str = Field(description="The client CA certificate (PEM): what a browser or a phone installs to trust it")
    crl: CrlInfo
