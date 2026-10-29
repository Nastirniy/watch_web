from typing import List, Literal, Optional

from pydantic import Field

from app.backend.api.v1.models.response.media import FILE_SAFETY_HEADERS, disposition_header
from app.backend.api.shared.openapi_docs import header
from app.backend.api.versioning import PublishedVersion
from app.backend.enums.routes import GuideRoutes
from app.backend.services.guides import GuideFileView, GuideView
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class GuideAudienceResponse(SecureBaseModel):
    """The node whose users the guide is for, named as it is now"""
    kind: Literal["post", "group"]
    id: int
    name: Optional[str] = Field(description="The name of the post or the group now; null only when its row is gone")


class GuideFileResponse(SecureBaseModel):
    """A file of a guide (one per language or format)"""
    id: int
    file_name: str = Field(description="The uploaded name, sanitised; the download is saved under it")
    content_type: str = Field(description="The canonical Content-Type of the file's format")
    size_bytes: int
    sha256: str
    language: Optional[str] = Field(description="Language tag (uk, en-US) or null")
    created_at: ResponseDatetime
    download_url: str = Field(description="Path of the download in this API version (GET, attachment)")
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


def guide_file_to_response(view: GuideFileView, version: PublishedVersion) -> GuideFileResponse:
    """version: the version of the request (request_version); download_url is its path of the file"""
    return GuideFileResponse(
        id=view.id, file_name=view.file_name, content_type=view.content_type, size_bytes=view.size_bytes,
        sha256=view.sha256, language=view.language, created_at=view.created_at,
        download_url=version.path(GuideRoutes.FILE_DOWNLOAD, guide_id=view.guide_id, file_id=view.id),
        is_deleted=view.is_deleted,
    )


def guide_to_response(view: GuideView, version: PublishedVersion) -> GuideResponse:
    return GuideResponse(
        id=view.id, title=view.title, description=view.description, sort_order=view.sort_order,
        audience=GuideAudienceResponse.model_validate(view.audience, from_attributes=True) if view.audience else None,
        files=[guide_file_to_response(item, version) for item in view.files], is_deleted=view.is_deleted,
        created_at=view.created_at, updated_at=view.updated_at,
    )


# Headers of GET /guides/{guide_id}/files/{file_id}/download
GUIDE_FILE_HEADERS = {
    "Content-Disposition": disposition_header("attachment", "file_name"),
    **FILE_SAFETY_HEADERS,
    "ETag": header("sha256 of the file, quoted"),
    "X-File-Hash": header("sha256 of the file"),
    "Accept-Ranges": header("bytes: the file can be read in parts with Range"),
    "Cache-Control": header("private, max-age=3600"),
}
