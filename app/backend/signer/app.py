"""The signing service application: a fixed profile, no caller-supplied names or extensions"""
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.backend.enums.media_types import ContentType
from app.backend.enums.routes import SIGNER_PROTOCOL_HEADER, SignerProtocol, SignerRoutes
from app.backend.pki.authority import load_ca, sha256_fingerprint
from app.backend.pki.crl import CrlEntry, build_crl
from app.backend.pki.profile import build_client_certificate, normalize_label
from app.backend.signer.models import (
    SignClientRequest, SignClientResponse, SignCrlRequest, SignCrlResponse, SignerHealthResponse,
)
from app.backend.signer.settings import SignerSettings

SUPPORTED_PROTOCOLS = [member.value for member in SignerProtocol]
MIN_KEY_BITS = 2048
MAX_KEY_BITS = 4096


class SigningRefused(Exception):
    """A request outside the profile; `reason` is public_key, label, validity_days, crl_number or crl_dates"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _public_key(pem: str) -> rsa.RSAPublicKey:
    try:
        key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError):
        raise SigningRefused("public_key") from None
    if not isinstance(key, rsa.RSAPublicKey) or not MIN_KEY_BITS <= key.key_size <= MAX_KEY_BITS:
        raise SigningRefused("public_key")
    return key


def create_app(settings: SignerSettings) -> FastAPI:
    """Load the CA now: a bad CA raises CaCheckError before anything is served"""
    ca_cert, ca_key = load_ca(settings.ca_cert_path, settings.ca_key_path, settings.ca_key_password)
    app = FastAPI(title="watch_web signer", openapi_url=None, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def protocol(request: Request, call_next):
        """Strict: a request without X-Signer-Protocol, or with a version this signer does not speak, is refused
        before anything else (RELEASE.md: api and pki-signer run one release tag)"""
        if request.headers.get(SIGNER_PROTOCOL_HEADER, "") not in {str(v) for v in SUPPORTED_PROTOCOLS}:
            logger.warning("refused a request with protocol {!r}; supported {}",
                           request.headers.get(SIGNER_PROTOCOL_HEADER), SUPPORTED_PROTOCOLS)
            return JSONResponse(
                status_code=409, media_type=ContentType.PROBLEM_JSON.value,
                content={"type": "about:blank", "title": "Protocol not supported", "status": 409,
                         "reason": "protocol_unsupported", "supported": SUPPORTED_PROTOCOLS},
            )
        return await call_next(request)

    @app.exception_handler(SigningRefused)
    async def refused(_: Request, exc: SigningRefused) -> JSONResponse:
        return JSONResponse(
            status_code=422, media_type=ContentType.PROBLEM_JSON.value,
            content={"type": "about:blank", "title": "Signing refused", "status": 422, "reason": exc.reason},
        )

    @app.post(SignerRoutes.SIGN_CLIENT.value, response_model=SignClientResponse)
    def sign_client(body: SignClientRequest) -> SignClientResponse:
        public_key = _public_key(body.public_key_pem)
        if normalize_label(body.label) != body.label:
            raise SigningRefused("label")
        if not 1 <= body.validity_days <= settings.max_validity_days:
            raise SigningRefused("validity_days")
        cert = build_client_certificate(ca_cert, ca_key, public_key, body.label, body.validity_days)
        serial = format(cert.serial_number, "x")
        logger.info("signed certificate serial={} cn={} not_after={} sha256={}", serial, body.label,
                    cert.not_valid_after_utc.isoformat(), sha256_fingerprint(cert))
        return SignClientResponse(
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), serial=serial)

    @app.post(SignerRoutes.SIGN_CRL.value, response_model=SignCrlResponse)
    def sign_crl(body: SignCrlRequest) -> SignCrlResponse:
        if body.crl_number < 1:
            raise SigningRefused("crl_number")
        this_update, next_update = _utc(body.this_update), _utc(body.next_update)
        latest = datetime.now(timezone.utc) + timedelta(days=settings.max_crl_days)
        if not this_update < next_update <= latest:
            raise SigningRefused("crl_dates")
        entries = [CrlEntry(int(entry.serial, 16), _utc(entry.revoked_at), entry.reason) for entry in body.entries]
        crl = build_crl(ca_cert, ca_key, body.crl_number, this_update, next_update, entries)
        logger.info("signed crl number={} entries={}", body.crl_number, len(entries))
        return SignCrlResponse(crl_pem=crl.public_bytes(serialization.Encoding.PEM).decode("ascii"))

    @app.get(SignerRoutes.HEALTH.value, response_model=SignerHealthResponse)
    def health() -> SignerHealthResponse:
        return SignerHealthResponse(
            subject=ca_cert.subject.rfc4514_string(), not_after=ca_cert.not_valid_after_utc,
            fingerprint_sha256=sha256_fingerprint(ca_cert), protocols=SUPPORTED_PROTOCOLS)

    return app
