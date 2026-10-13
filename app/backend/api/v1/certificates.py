from typing import Annotated, List

from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, Query
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.query.certificates import CertificateFilterParams
from app.backend.api.v1.models.response.certificates import (
    CertificateAuthorityResponse, CertificateResponse, CrlInfo, certificate_to_response,
)
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.routes import CertificateRoutes
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.certificate_filter import CertificateFilterService
from app.backend.models.user_certificate import UserCertificate
from app.backend.pki.authority import load_ca_certificate, sha256_fingerprint
from app.backend.services.crl_publisher import crl_status

router = APIRouter(prefix=CertificateRoutes.BASE.value, tags=[CertificateRoutes.tag()])


@router.get(
    CertificateRoutes.ROOT.value,
    response_model=List[CertificateResponse],
    responses=problems(AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, *GUARDED),
)
async def list_certificates(
        filters: Annotated[CertificateFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW])),
):
    """The certificates of non-archived users whose home post is where both certificate.view and user.view are in
    effect (users without a home post: with both at the root), revoked and expired ones included. post_id narrows
    the list to one post, where both must be in effect (403 otherwise)."""
    scope = AccessControlService.scope_for_all(auth, Permissions.CERTIFICATE_VIEW, Permissions.USER_VIEW)
    if filters.post_id is not None:
        for permission in (Permissions.CERTIFICATE_VIEW, Permissions.USER_VIEW):
            AccessControlService.check_post(auth, permission, filters.post_id, resource_type="certificate")
        scope = frozenset({filters.post_id})
    query = CertificateFilterService.apply_filters(select(UserCertificate), filters, home_post_ids=scope)
    return [certificate_to_response(row) for row in (await session.exec(query)).all()]


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
