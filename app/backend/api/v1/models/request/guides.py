import re
from typing import Literal, Optional

from pydantic import Field, PositiveInt, field_validator

from app.backend.enums.errors import FieldErrorCode
from app.backend.errors.coded import invalid
from app.backend.errors.problems import field_problem
from app.backend.utils.validators import SecureBaseModel, SecurityValidators, not_null

TITLE_MAX = 200
DESCRIPTION_MAX = 2000


TITLE_DESCRIPTION = ("Plain text, one line, 1 to 200 characters after trimming (empty: 422 value_empty). Untrusted "
                     "text: render it as text")
DESCRIPTION_DESCRIPTION = ("Plain text, line breaks kept, at most 2000 characters; empty after trimming is stored as "
                           "null. Untrusted text: render it as text")


# Guide text is natural plain text ("How to create an observation"): SecurityValidators.plain_text, the rule of every
# free-text field
def _title(value: str) -> str:
    """No min_length on the field: a title that strips or decodes to "" ("   ", "&nbsp;") answers value_empty"""
    title = SecurityValidators.plain_text(value, max_length=TITLE_MAX)
    if not title:
        raise invalid(FieldErrorCode.VALUE_EMPTY)
    return title


def _description(value: Optional[str]) -> Optional[str]:
    """An empty description (after trimming) is no description: null"""
    if value is None:
        return None
    return SecurityValidators.plain_text(value, max_length=DESCRIPTION_MAX, multiline=True) or None


class GuideAudienceRef(SecureBaseModel):
    """The node whose users the guide is for: a post, or a scope group (a corps, a brigade, any group) and
    everything below it"""
    kind: Literal["post", "group"] = Field(description="post: one post; group: a scope group")
    id: PositiveInt = Field(description="Id of the post or the group")


class GuideCreate(SecureBaseModel):
    title: str = Field(..., max_length=TITLE_MAX, description=TITLE_DESCRIPTION)
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX, description=DESCRIPTION_DESCRIPTION)
    sort_order: int = Field(default=0, ge=-1_000_000, le=1_000_000,
                            description="Position in the list: guides are listed by sort_order, then title")
    audience: Optional[GuideAudienceRef] = Field(
        default=None, description="Who the guide is for; null or left out: everyone. The node must exist and be "
                                  "live (404 post_not_found / scope_group_not_found)")

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        return _title(v)

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        return _description(v)


class GuideUpdate(SecureBaseModel):
    """Only the fields sent change. description: null clears it; audience: null makes the guide for everyone, left
    out keeps it"""
    title: Optional[str] = Field(default=None, max_length=TITLE_MAX, description=TITLE_DESCRIPTION)
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX, description=DESCRIPTION_DESCRIPTION)
    sort_order: Optional[int] = Field(default=None, ge=-1_000_000, le=1_000_000)
    audience: Optional[GuideAudienceRef] = Field(default=None)

    refuse_null = not_null("title", "sort_order")

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: Optional[str]) -> Optional[str]:
        return _title(v) if v is not None else v

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        return _description(v)


# The form field language of POST /guides/{guide_id}/files (multipart: the file is a File() parameter, so the field is
# a Form() parameter of the route, not a model; FastAPI documents a form model with a file as urlencoded)
LANGUAGE_MAX = 8
LANGUAGE_DESCRIPTION = ("Language of the file, a BCP 47 style tag: 2 or 3 letters, then optional subtags of 1 to 8 "
                        "letters or digits joined by hyphens, at most 8 characters (uk, en, en-US, zh-Hant, es-419); "
                        "empty or left out: none")
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*", re.ASCII)


def guide_file_language(value: Optional[str]) -> Optional[str]:
    """The language tag as sent (tags are case-insensitive: it is not rewritten), None for empty; anything else is
    422 language_tag_invalid at #/language. Not a full BCP 47 parser: the shape of language-subtags, no registry."""
    if value is None or value == "":
        return None
    if not _LANGUAGE_TAG.fullmatch(value):
        raise field_problem(FieldErrorCode.LANGUAGE_TAG_INVALID, ("body", "language"))
    return value
