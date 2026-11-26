"""Client of the signing service (app/backend/signer): HTTP over a Unix socket.

The API holds no CA key. It sends public keys and CRL contents and receives signed certificates and CRLs.
A signer that does not answer is SignerUnavailable; one that refuses or fails is SignerRefused (a bug or a
misconfiguration, so it reaches the 500 handler with its status and reason in the log).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import httpx
from loguru import logger

from app.backend.config import settings
from app.backend.enums.routes import SIGNER_PROTOCOL_HEADER, SignerProtocol, SignerRoutes
from app.backend.pki.crl import CrlEntry
from app.backend.utils.datetime_utils import as_utc

SIGNER_TIMEOUT_SECONDS = 10
PROTOCOL = SignerProtocol.V1
_BASE_URL = "http://signer"

# Tests install an in-process signer here (httpx.ASGITransport); None means the Unix socket of settings.pki
_transport_override: Optional[httpx.AsyncBaseTransport] = None


class SignerUnavailable(Exception):
    """The signer does not answer (socket missing, connection refused, timeout)"""


class SignerProtocolMismatch(SignerUnavailable):
    """The signer speaks another protocol version: api and pki-signer run different releases (503 like a signer
    that does not answer; the log line names both versions)"""


class SignerRefused(RuntimeError):
    """The signer answered with an error status; the message holds the status and the reason"""


@dataclass(frozen=True)
class SignedCertificate:
    certificate_pem: str
    serial: str          # lower-case hex


@dataclass(frozen=True)
class SignerStatus:
    subject: str
    not_after: datetime
    fingerprint_sha256: str


class SignerClient:
    def __init__(self, transport: httpx.AsyncBaseTransport):
        self._transport = transport

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        try:
            async with httpx.AsyncClient(
                    transport=self._transport, base_url=_BASE_URL, timeout=SIGNER_TIMEOUT_SECONDS) as client:
                response = await client.request(method, path, json=body,
                                                headers={SIGNER_PROTOCOL_HEADER: str(PROTOCOL.value)})
        except httpx.TransportError as exc:
            raise SignerUnavailable(type(exc).__name__) from None
        if response.status_code >= 400:
            answer = {}
            try:
                answer = response.json()
            except ValueError:
                pass
            reason = str(answer.get("reason", "")) if isinstance(answer, dict) else ""
            if response.status_code == 409 and reason == "protocol_unsupported":
                logger.error(f"Signer protocol mismatch: the api speaks {PROTOCOL.value}, the signer supports "
                             f"{answer.get('supported')}. Fix: run one release tag for api and pki-signer "
                             f"(deploy/release.sh status).")
                raise SignerProtocolMismatch("protocol_unsupported")
            raise SignerRefused(f"signer answered {response.status_code} for {path}: {reason}")
        return response.json()

    async def sign_client(self, public_key_pem: str, label: str, validity_days: int) -> SignedCertificate:
        answer = await self._call("POST", SignerRoutes.SIGN_CLIENT.value, {
            "public_key_pem": public_key_pem, "label": label, "validity_days": validity_days})
        return SignedCertificate(certificate_pem=answer["certificate_pem"], serial=answer["serial"])

    async def sign_crl(self, crl_number: int, this_update: datetime, next_update: datetime,
                       entries: Sequence[CrlEntry]) -> str:
        answer = await self._call("POST", SignerRoutes.SIGN_CRL.value, {
            "crl_number": crl_number,
            "this_update": as_utc(this_update).isoformat(),
            "next_update": as_utc(next_update).isoformat(),
            "entries": [{"serial": format(entry.serial, "x"), "revoked_at": as_utc(entry.revoked_at).isoformat(),
                         "reason": entry.reason.value} for entry in entries],
        })
        return answer["crl_pem"]

    async def health(self) -> SignerStatus:
        answer = await self._call("GET", SignerRoutes.HEALTH.value)
        return SignerStatus(subject=answer["subject"], not_after=datetime.fromisoformat(answer["not_after"]),
                            fingerprint_sha256=answer["fingerprint_sha256"])


def use_signer_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None:
    """Tests: talk to this transport instead of the Unix socket (None restores the socket)"""
    global _transport_override
    _transport_override = transport


def signer_client() -> SignerClient:
    transport = _transport_override or httpx.AsyncHTTPTransport(uds=settings.pki.signer_socket)
    return SignerClient(transport)
