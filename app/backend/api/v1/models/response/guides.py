from typing import List, Literal, Optional

from pydantic import Field

from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class GuideAudienceResponse(SecureBaseModel):
    """The node whose users the guide is for, named as it is now"""
    kind: Literal["post", "group"]
    id: int
    name: Optional[str] = Field(description="The name of the post or the group now; null only when its row is gone")


class GuideFileResponse(SecureBaseModel):
    """A file of a guide (one per language or format)"""
    id: int
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    language: Optional[str]
    created_at: ResponseDatetime
    is_deleted: bool


class GuideResponse(SecureBaseModel):
    id: int
    title: str
    description: Optional[str]
    sort_order: int
    audience: Optional[GuideAudienceResponse] = Field(description="null: the guide is for everyone")
    files: List[GuideFileResponse] = Field(description="Its files, oldest first; archived ones only with "
                                                       "include_deleted")
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
