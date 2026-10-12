from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.response.certificates import CertificateAuthorityResponse, CrlInfo
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import CertificateRoutes
from app.backend.errors.openapi import problems
from app.backend.errors.problems import ApiError
from app.backend.pki.authority import load_ca_certificate, sha256_fingerprint
from app.backend.services.crl_publisher import crl_status

router = APIRouter(prefix=CertificateRoutes.BASE.value, tags=[CertificateRoutes.tag()])


@router.get(
    CertificateRoutes.AUTHORITY.value,
    response_model=CertificateAuthorityResponse,
    responses=problems(ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED),
)
async def get_certificate_authority(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
):
    """The client certificate authority: its certificate and the state of the published CRL. 503 when the server
    has no PKI configured."""
    if not settings.pki.is_configured():
        raise ApiError(ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED)
    ca_cert = load_ca_certificate(settings.pki.ca_cert_path)
    status = await crl_status(session)
    return CertificateAuthorityResponse(
        subject=ca_cert.subject.rfc4514_string(),
        not_after=ca_cert.not_valid_after_utc,
        fingerprint_sha256=sha256_fingerprint(ca_cert),
        certificate_pem=ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        crl=CrlInfo(this_update=status.this_update, next_update=status.next_update,
                    revoked_count=status.revoked_count),
    )
