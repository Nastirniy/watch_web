import re
from typing import List, Optional

from pydantic import Field, field_validator, model_validator

from app.backend.enums.general import MediaType
from app.backend.utils.validators import SecureBaseModel, SecurityValidators, not_null
from app.backend.errors.coded import invalid
from app.backend.enums.errors import FieldErrorCode
from app.backend.services.files.formats import FORBIDDEN_CONTENT_TYPES, OPAQUE_CONTENT_TYPES, content_type_allowed

ACCEPTED_MEDIA_TYPES = tuple(member.value for member in MediaType)
# RFC 6838 type/subtype, lower case only
CONTENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


def _clean_content_type(value: str) -> str:
    value = value.strip().lower()
    if not CONTENT_TYPE_RE.match(value):
        raise invalid(FieldErrorCode.CONTENT_TYPE_INVALID)
    if value in FORBIDDEN_CONTENT_TYPES:
        raise invalid(FieldErrorCode.CONTENT_TYPE_FORBIDDEN, content_type=value)
    # Opaque types name no format, and as an alias the sniffer's answer for unknown bytes would pass any file
    if value in OPAQUE_CONTENT_TYPES:
        raise invalid(FieldErrorCode.CONTENT_TYPE_OPAQUE, content_type=value)
    return value


def _clean_extensions(values: List[str]) -> List[str]:
    cleaned: List[str] = []
    for raw in values:
        ext = raw.strip().lower()
        if not ext.startswith("."):
            ext = "." + ext
        if not EXTENSION_RE.match(ext):
            raise invalid(FieldErrorCode.EXTENSION_INVALID)
        if ext not in cleaned:
            cleaned.append(ext)
    if not cleaned:
        raise invalid(FieldErrorCode.EXTENSIONS_REQUIRED)
    return cleaned


def _clean_aliases(values: List[str]) -> List[str]:
    cleaned: List[str] = []
    for raw in values:
        alias = _clean_content_type(raw)
        if alias not in cleaned:
            cleaned.append(alias)
    return cleaned


class MediaFormatCreate(SecureBaseModel):
    media_type: str = Field(..., description="image, video, audio or document")
    content_type: str = Field(..., min_length=3, max_length=100, description="Canonical Content-Type, e.g. video/mp4")
    label: str = Field(..., min_length=1, max_length=255)
    extensions: List[str] = Field(..., min_length=1, max_length=20, description="File extensions with the dot, e.g. ['.mp4', '.m4v']")
    aliases: List[str] = Field(default_factory=list, max_length=20, description="Other MIME spellings clients or detectors may send")
    max_file_size_bytes: int = Field(..., ge=1, description="Largest accepted file for this format, in bytes (capped by storage.max_file_size_gb)")
    is_active: bool = Field(True, description="Inactive formats are kept but not accepted")
    sort_order: int = Field(0, ge=0)

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ACCEPTED_MEDIA_TYPES:
            raise invalid(FieldErrorCode.MEDIA_TYPE_NOT_ACCEPTED, accepted=list(ACCEPTED_MEDIA_TYPES))
        return v

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        return _clean_content_type(v)

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        return SecurityValidators.plain_text(v, max_length=255)

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, v: List[str]) -> List[str]:
        return _clean_extensions(v)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, v: List[str]) -> List[str]:
        return _clean_aliases(v)

    @model_validator(mode="after")
    def content_type_matches_media_type(self):
        if not content_type_allowed(self.media_type, self.content_type):
            raise invalid(FieldErrorCode.CONTENT_TYPE_OUTSIDE_MEDIA_TYPE, media_type=self.media_type)
        return self


class MediaFormatUpdate(SecureBaseModel):
    """Partial update. Archiving and restoring go through POST /{format_id}/archive and /restore."""
    media_type: Optional[str] = Field(None)
    content_type: Optional[str] = Field(None, min_length=3, max_length=100)
    label: Optional[str] = Field(None, min_length=1, max_length=255)
    extensions: Optional[List[str]] = Field(None, min_length=1, max_length=20)
    aliases: Optional[List[str]] = Field(None, max_length=20)
    max_file_size_bytes: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)

    refuse_null = not_null(
        "media_type", "content_type", "label", "extensions", "aliases", "max_file_size_bytes", "is_active", "sort_order"
    )

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in ACCEPTED_MEDIA_TYPES:
            raise invalid(FieldErrorCode.MEDIA_TYPE_NOT_ACCEPTED, accepted=list(ACCEPTED_MEDIA_TYPES))
        return v

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: Optional[str]) -> Optional[str]:
        return _clean_content_type(v) if v is not None else v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: Optional[str]) -> Optional[str]:
        return SecurityValidators.plain_text(v, max_length=255) if v is not None else v

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _clean_extensions(v) if v is not None else v

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _clean_aliases(v) if v is not None else v
