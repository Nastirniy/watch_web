from typing import Optional

from pydantic import Field

from app.backend.utils.validators import PlainTextModel, ResponseDatetime


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
