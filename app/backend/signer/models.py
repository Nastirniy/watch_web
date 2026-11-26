"""Request and response bodies of the signing service"""
import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.backend.enums.general import RevocationReason

_HEX = re.compile(r"[0-9a-fA-F]{1,40}")


class SignClientRequest(BaseModel):
    # a request never adds a name or an extension (pki/profile.py): any other field is refused, not ignored
    model_config = ConfigDict(extra="forbid")

    public_key_pem: str
    label: str
    validity_days: int


class SignClientResponse(BaseModel):
    certificate_pem: str
    serial: str


class CrlEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial: str
    revoked_at: datetime
    reason: RevocationReason

    @field_validator("serial")
    @classmethod
    def serial_is_hex(cls, value: str) -> str:
        if not _HEX.fullmatch(value):
            raise ValueError("serial must be 1..40 hex digits")
        return value


class SignCrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crl_number: int
    this_update: datetime
    next_update: datetime
    entries: list[CrlEntryIn]


class SignCrlResponse(BaseModel):
    crl_pem: str


class SignerHealthResponse(BaseModel):
    subject: str
    not_after: datetime
    fingerprint_sha256: str
    protocols: list[int]
