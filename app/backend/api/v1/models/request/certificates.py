from typing import Optional

from fastapi import UploadFile
from pydantic import ConfigDict, Field, field_validator

from app.backend.config import settings
from app.backend.enums.errors import FieldErrorCode
from app.backend.errors.coded import invalid
from app.backend.pki.profile import LABEL_MAX_LENGTH, normalize_label
from app.backend.utils.validators import SecureBaseModel

_LABEL = (f"Readable name of the certificate, 1 to {LABEL_MAX_LENGTH} characters after trimming, no control "
          f"characters: the CN of the certificate and the name browsers show in the certificate picker "
          f"(for example 'Petrenko I. - post 2 - laptop')")


def _checked_label(value: str) -> str:
    label = normalize_label(value)
    if label is None:
        raise invalid(FieldErrorCode.CERTIFICATE_LABEL_INVALID)
    return label


class CertificateIssueRequest(SecureBaseModel):
    """Body of POST /users/{user_id}/certificates"""
    label: str = Field(description=_LABEL)
    validity_days: Optional[int] = Field(
        default=None, ge=1,
        description="Days the certificate is valid (default settings.pki.validity_days, 365; at most "
                    "settings.pki.max_validity_days, 730 by default: validity_days_too_long otherwise)")

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        return _checked_label(v)

    @field_validator("validity_days")
    @classmethod
    def validate_validity_days(cls, v: Optional[int]) -> Optional[int]:
        # The limit is a setting: a code of its own instead of a static le
        if v is not None and v > settings.pki.max_validity_days:
            raise invalid(FieldErrorCode.VALIDITY_DAYS_TOO_LONG, max_days=settings.pki.max_validity_days)
        return v


class CertificateUploadForm(SecureBaseModel):
    """Form of POST /users/{user_id}/certificates/upload (multipart/form-data). Unknown form fields are ignored, as
    with plain Form() params."""
    model_config = ConfigDict(extra="ignore")

    file: UploadFile = Field(description="The certificate file, PEM or DER")
    label: str = Field(description=_LABEL)

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        return _checked_label(v)
