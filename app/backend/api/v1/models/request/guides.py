from typing import Literal, Optional

from pydantic import Field, PositiveInt, field_validator

from app.backend.utils.validators import SecureBaseModel, SecurityValidators, not_null

TITLE_MAX = 200
DESCRIPTION_MAX = 2000


def _title(value: str) -> str:
    return SecurityValidators.sanitize_string(value.strip(), max_length=TITLE_MAX)


def _description(value: Optional[str]) -> Optional[str]:
    return SecurityValidators.sanitize_string(value.strip(), max_length=DESCRIPTION_MAX) if value is not None else value


class GuideAudienceRef(SecureBaseModel):
    """The node whose users the guide is for: a post, or a scope group (a corps, a brigade, any group) and
    everything below it"""
    kind: Literal["post", "group"] = Field(description="post: one post; group: a scope group")
    id: PositiveInt = Field(description="Id of the post or the group")


class GuideCreate(SecureBaseModel):
    title: str = Field(..., min_length=1, max_length=TITLE_MAX)
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX, description="Plain text")
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
    title: Optional[str] = Field(default=None, min_length=1, max_length=TITLE_MAX)
    description: Optional[str] = Field(default=None, max_length=DESCRIPTION_MAX, description="Plain text")
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
