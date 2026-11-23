"""User guides: the help section, titled entries with their files (services/guides.py).

Reading needs only an authenticated user (require() with no permission): there is no guide.view, so the rule that a
write route requires X.view as well does not apply here, reading is open. Writes need the system permissions
(held through a system role): create guide.create, PATCH guide.update, archive and restore guide.archive.
include_deleted (archived guides and files too) requires guide.archive.

The audience ({kind: post | group, id}, null: everyone) is stored as the scope key of the node; an unknown or
archived node answers 404 post_not_found / scope_group_not_found, as the assignment API does.

Who sees a guide (services/guides.py guide_visible, plan decision 8): a guide without an audience, everyone; a guide
for a node, the managers (guide.update) and the users related to the node: their home post or a node where they
hold a tree role lies on one path with it (inside it or above it). An archived audience node hides the guide from
everyone but managers. A guide the caller does not see is left out of the list (in SQL, before the page is cut) and
answers 404 guide_not_found on every route of the guide and of its files, as an unknown guide does. Writes need
their permission first (403 from require()), then the same lookup.

Nothing is removed: POST /{guide_id}/archive hides a guide (is_deleted=true), POST /{guide_id}/restore brings it
back. An archived guide answers 404 on update, a live one 409 on restore. Every write and its audit row
(user_guide.*, system-wide: no scope) are one transaction.

Files (services/files/store.py): POST /{guide_id}/files uploads one (multipart, guide.update), held to a document
format of the registry as a media upload is to its formats (413 / 415 with Accept-Post; a text/* format must be
UTF-8, 415 document_text_invalid); GET .../download serves it as an attachment (stored_file_response); archive and
restore need guide.archive. A file is never deleted: archived, it stays on disk, is left out of the guide's files
and its download answers 404, both unless include_deleted (guide.archive). The upload locks the guide row first,
then inserts the file row, writes the folder and commits with the audit row (user_guide_file.*); when that
transaction fails the folder is removed.
"""
from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.shared.helpers.media_helper import if_none_match_of, stored_file_response
from app.backend.api.shared.helpers.upload_helper import check_guide_format, format_rejected, store_rejected, too_large
from app.backend.api.shared.openapi_docs import empty_response, file_response
from app.backend.api.v1.models.query.guides import GuideFilterParams, GuideReadParams
from app.backend.api.v1.models.request.guides import (
    LANGUAGE_DESCRIPTION, LANGUAGE_MAX, GuideCreate, GuideUpdate, guide_file_language,
)
from app.backend.api.v1.models.response.guides import (
    GUIDE_FILE_HEADERS, GuideFileResponse, GuideResponse, guide_file_to_response, guide_to_response,
)
from app.backend.api.v1.models.response.media import (
    FILE_NOT_MODIFIED, FILE_NOT_MODIFIED_HEADERS, PARTIAL_FILE_HEADERS,
)
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.audit import AuditEvent
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.media_types import ContentType, Disposition
from app.backend.enums.routes import GuideRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.models.user_guide import UserGuide, UserGuideFile
from app.backend.services import guides as guides_service
from app.backend.services.audit import create_audit_log
from app.backend.services.files import formats
from app.backend.services.files import store as file_store
from app.backend.services.files.formats import FileTooLarge, UnsupportedMediaFormat
from app.backend.utils.datetime_utils import get_default_timezone_datetime
from app.backend.enums.events import Dictionary
from app.backend.services.events import publish_dictionary_changed

router = APIRouter(prefix=GuideRoutes.BASE.value, tags=[GuideRoutes.tag()])


def _response(view: guides_service.GuideView, version: PublishedVersion) -> GuideResponse:
    return guide_to_response(view, version)


def _file_response(row: UserGuideFile, version: PublishedVersion) -> GuideFileResponse:
    return guide_file_to_response(guides_service.file_view(row), version)


async def _guide_or_404(session: AsyncSession, guide_id: int, auth: AuthContext, include_deleted: bool = False,
                        lock: bool = False) -> UserGuide:
    """The guide when the caller sees it; unknown, archived (without include_deleted) or not for the caller: 404"""
    try:
        return await guides_service.get_guide(session, guide_id, auth, include_deleted, lock=lock)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


async def _audience_key(session: AsyncSession, audience) -> str:
    try:
        return await guides_service.audience_key(session, audience.kind, audience.id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


@router.get(
    GuideRoutes.ROOT.value,
    response_model=List[GuideResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION),
)
async def list_guides(
        filters: Annotated[GuideFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
        version: PublishedVersion = Depends(request_version),
):
    """The guides the caller sees with their files, by sort_order then title: those without an audience, and
    those whose audience node lies on one path with the caller's home post or with a node of the caller's tree
    roles (managers, guide.update: every guide). Archived guides and files only with include_deleted (requires
    guide.archive)."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.GUIDE_ARCHIVE)
    views = await guides_service.list_guides(session, guides_service.GuideFilterService.criteria(filters), auth)
    return [_response(view, version) for view in views]


@router.post(
    GuideRoutes.ROOT.value,
    response_model=GuideResponse,
    status_code=201,
    responses=problems(ErrorCode.POST_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED),
)
async def create_guide(
        request: Request,
        data: GuideCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_CREATE])),
        version: PublishedVersion = Depends(request_version),
):
    """Add a guide (without files). audience: an unknown or archived post or group answers 404."""
    key = await _audience_key(session, data.audience) if data.audience is not None else None
    now = get_default_timezone_datetime()
    guide = UserGuide(title=data.title, description=data.description, sort_order=data.sort_order, audience_key=key,
                      created_at=now, updated_at=now, created_by=auth.user.id, updated_by=auth.user.id)
    session.add(guide)
    # One transaction: the row, its audit row, one commit
    await session.flush()
    await session.refresh(guide)
    await create_audit_log(
        session, AuditEvent.USER_GUIDE_CREATE,
        params={"guide_title": guide.title},
        resource_id=guide.id,
        user_id=auth.user.id,
        old_values=None,
        new_values=guide.model_dump(),
        **client_info(request, auth),
    )
    publish_dictionary_changed(session, Dictionary.GUIDES)
    await session.commit()
    await session.refresh(guide)
    return _response(await guides_service.guide_view(session, guide), version)


@router.get(
    GuideRoutes.GUIDE_BY_ID.value,
    response_model=GuideResponse,
    responses=problems(ErrorCode.GUIDE_NOT_FOUND, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION),
)
async def get_guide(
        guide_id: int,
        params: Annotated[GuideReadParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
        version: PublishedVersion = Depends(request_version),
):
    """One guide with its files. A guide whose audience the caller is not in: 404, as an unknown one. An archived
    guide only with include_deleted (requires guide.archive), which also lists its archived files; otherwise 404."""
    AccessControlService.check_include_deleted(auth, params, Permissions.GUIDE_ARCHIVE)
    guide = await _guide_or_404(session, guide_id, auth, include_deleted=params.include_deleted)
    return _response(await guides_service.guide_view(session, guide, include_deleted_files=params.include_deleted),
                     version)


@router.patch(
    GuideRoutes.GUIDE_BY_ID.value,
    response_model=GuideResponse,
    responses=problems(ErrorCode.GUIDE_NOT_FOUND, ErrorCode.POST_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_FOUND,
                       *GUARDED),
)
async def update_guide(
        request: Request,
        guide_id: int,
        data: GuideUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_UPDATE])),
        version: PublishedVersion = Depends(request_version),
):
    """Change the fields sent. description: null clears it; audience: null makes the guide for everyone, left out
    keeps it (an unknown or archived node: 404). An archived guide: 404 (restore it first)."""
    guide = await _guide_or_404(session, guide_id, auth, lock=True)
    sent = data.model_fields_set
    changes = {field: getattr(data, field) for field in ("title", "description", "sort_order") if field in sent}
    if "audience" in sent:
        changes["audience_key"] = await _audience_key(session, data.audience) if data.audience is not None else None
    if not changes:     # an empty body changes nothing and writes no audit row
        return _response(await guides_service.guide_view(session, guide), version)

    old_values = guide.model_dump()
    for field, value in changes.items():
        setattr(guide, field, value)
    guide.updated_at = get_default_timezone_datetime()
    guide.updated_by = auth.user.id
    session.add(guide)
    await session.flush()
    await create_audit_log(
        session, AuditEvent.USER_GUIDE_UPDATE,
        params={"guide_title": guide.title},
        resource_id=guide.id,
        user_id=auth.user.id,
        old_values=old_values,
        new_values=guide.model_dump(),
        **client_info(request, auth),
    )
    publish_dictionary_changed(session, Dictionary.GUIDES)
    await session.commit()
    await session.refresh(guide)
    return _response(await guides_service.guide_view(session, guide), version)


async def _set_archived(request: Request, session: AsyncSession, auth: AuthContext, guide: UserGuide,
                        archived: bool, version: PublishedVersion) -> GuideResponse:
    """Archive or restore the locked guide with its audit row, one transaction"""
    old_values = guide.model_dump()
    guide.is_deleted = archived
    guide.updated_at = get_default_timezone_datetime()
    guide.updated_by = auth.user.id
    session.add(guide)
    await create_audit_log(
        session, AuditEvent.USER_GUIDE_ARCHIVE if archived else AuditEvent.USER_GUIDE_RESTORE,
        params={"guide_title": guide.title},
        resource_id=guide.id,
        user_id=auth.user.id,
        old_values=old_values,
        new_values=guide.model_dump(),
        **client_info(request, auth),
    )
    publish_dictionary_changed(session, Dictionary.GUIDES)
    await session.commit()
    await session.refresh(guide)
    return _response(await guides_service.guide_view(session, guide), version)


@router.post(
    GuideRoutes.GUIDE_ARCHIVE.value,
    response_model=GuideResponse,
    responses=problems(ErrorCode.GUIDE_NOT_FOUND, *GUARDED),
)
async def archive_guide(
        request: Request,
        guide_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_ARCHIVE])),
        version: PublishedVersion = Depends(request_version),
):
    """Archive a guide (is_deleted=true). Nothing is removed: its files stay, the guide is hidden from lists unless
    include_deleted=true. Undo with POST /{guide_id}/restore. Already archived, unknown or not for the caller: 404."""
    guide = await _guide_or_404(session, guide_id, auth, lock=True)
    return await _set_archived(request, session, auth, guide, archived=True, version=version)


@router.post(
    GuideRoutes.GUIDE_RESTORE.value,
    response_model=GuideResponse,
    responses=problems(ErrorCode.GUIDE_NOT_FOUND, ErrorCode.GUIDE_NOT_ARCHIVED, *GUARDED),
)
async def restore_guide(
        request: Request,
        guide_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_ARCHIVE])),
        version: PublishedVersion = Depends(request_version),
):
    """Restore an archived guide (is_deleted=false). Unknown or not for the caller: 404. Not archived: 409."""
    guide = await _guide_or_404(session, guide_id, auth, include_deleted=True, lock=True)
    if not guide.is_deleted:
        raise ApiError(ErrorCode.GUIDE_NOT_ARCHIVED)
    return await _set_archived(request, session, auth, guide, archived=False, version=version)


# ---------------------------------------------------------------------------------------------------------- files

async def _file_or_404(session: AsyncSession, guide_id: int, file_id: int, include_deleted: bool = False,
                       lock: bool = False) -> UserGuideFile:
    try:
        return await guides_service.get_guide_file(session, guide_id, file_id, include_deleted, lock=lock)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


def _file_problem(exc: Exception, registry: formats.FormatRegistry, spec: formats.FormatSpec) -> ApiError:
    """The problem of a file the store refused: 413 with the limit the store held it to, 415 with Accept-Post, or the
    store's own code"""
    if isinstance(exc, FileTooLarge):
        return too_large(spec, exc.actual_bytes, limit=exc.limit_bytes)
    if isinstance(exc, UnsupportedMediaFormat):
        return format_rejected(registry, exc)
    return store_rejected(exc)


def _file_params(guide: UserGuide, row: UserGuideFile) -> dict:
    return {"guide_title": guide.title, "file_name": row.file_name, "size_bytes": row.size_bytes,
            "sha256": row.sha256}


@router.post(
    GuideRoutes.FILES.value,
    response_model=GuideFileResponse,
    status_code=201,
    responses=problems(
        ErrorCode.GUIDE_NOT_FOUND, ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.DOCUMENT_TEXT_INVALID,
        ErrorCode.MEDIA_STORE_FAILED, *GUARDED,
    ),
)
async def upload_guide_file(
        request: Request,
        guide_id: int,
        file: UploadFile = File(..., description="The document, of a format of media type document"),
        language: Optional[str] = Form(None, max_length=LANGUAGE_MAX, description=LANGUAGE_DESCRIPTION),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_UPDATE])),
        version: PublishedVersion = Depends(request_version),
):
    """Add a file to a guide (multipart: file, optional language). The file must be of a document format: the
    extension, the declared Content-Type and the bytes are checked (415 with Accept-Post), its size against the
    format's limit (413 media_file_too_large with max_file_size_bytes); a text format (Markdown, plain text, CSV)
    must be UTF-8 without NUL bytes (415 document_text_invalid). The body is spooled to disk in 1 MB chunks, never
    held in memory. Stored under the format's canonical Content-Type and the sanitised file name. An archived or
    unknown guide: 404."""
    language = guide_file_language(language)
    await _guide_or_404(session, guide_id, auth)       # a plain read: 404 before the body is copied
    registry = await formats.load_registry(session)
    spec = check_guide_format(registry, file.filename or "", file.content_type)
    if file.size and file.size > spec.effective_limit_bytes:
        raise too_large(spec, file.size)

    spooled = await file_store.spool_upload(file, Path(settings.storage.temp_path))
    try:
        # The slow file work first (sniff, text rule, sha256), outside the write: no lock is held meanwhile
        try:
            vetted = await file_store.vet_guide_file(session, spooled, file.filename or "")
        except (FileTooLarge, CodedError) as exc:
            raise _file_problem(exc, registry, spec)
        # The write: the guide row first (an archive of the guide waits, or this answers 404), then the file row
        guide = await _guide_or_404(session, guide_id, auth, lock=True)
        try:
            row = await file_store.store_guide_file(session, guide, vetted, language, auth.user.id)
        except CodedError as exc:
            raise _file_problem(exc, registry, spec)
        # One transaction: the row, its audit row, one commit; when it fails the folder goes too
        try:
            await create_audit_log(
                session, AuditEvent.USER_GUIDE_FILE_UPLOAD,
                params=_file_params(guide, row),
                resource_id=row.id,
                user_id=auth.user.id,
                old_values=None,
                new_values=row.model_dump(),
                **client_info(request, auth),
            )
            publish_dictionary_changed(session, Dictionary.GUIDES)
            await session.commit()
        except BaseException:
            await file_store.discard_guide_file(session, row)
            raise
    finally:
        spooled.unlink(missing_ok=True)
    return _file_response(row, version)


@router.get(
    GuideRoutes.FILE_DOWNLOAD.value,
    response_class=Response,
    responses={
        **file_response("The file (attachment), in its format's Content-Type (text types with charset)",
                        [ContentType.ANY], GUIDE_FILE_HEADERS),
        **file_response("The requested byte range (Range header)", [ContentType.ANY], PARTIAL_FILE_HEADERS, status=206),
        **empty_response(304, FILE_NOT_MODIFIED, FILE_NOT_MODIFIED_HEADERS),
        **problems(
            ErrorCode.GUIDE_NOT_FOUND, ErrorCode.GUIDE_FILE_NOT_FOUND, ErrorCode.MEDIA_PATH_UNRESOLVED,
            ErrorCode.RANGE_NOT_SATISFIABLE, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        ),
    },
)
async def download_guide_file(
        request: Request,
        guide_id: int,
        file_id: int,
        params: Annotated[GuideReadParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
):
    """Download a file of a guide as an attachment under its file name (nginx serves it via X-Accel-Redirect;
    otherwise the API, with Range support). An archived file, or a file of an archived guide, only with
    include_deleted (requires guide.archive); otherwise 404. A guide whose audience the caller is not in: 404
    guide_not_found, as an unknown one. A file gone from disk: 404 guide_file_not_found. ETag and Repr-Digest name the
    file, on a 206 as well. If-None-Match with the ETag (or *) answers 304 without the file, after those checks;
    If-Range with the ETag works as usual."""
    AccessControlService.check_include_deleted(auth, params, Permissions.GUIDE_ARCHIVE)
    await _guide_or_404(session, guide_id, auth, include_deleted=params.include_deleted)
    row = await _file_or_404(session, guide_id, file_id, include_deleted=params.include_deleted)
    response = stored_file_response(
        file_store.guide_file_location(row), content_type=row.content_type, filename=row.file_name,
        disposition=Disposition.ATTACHMENT, etag=row.sha256, range_header=request.headers.get("range"),
        if_none_match=if_none_match_of(request), missing=ErrorCode.GUIDE_FILE_NOT_FOUND,
    )
    response.headers["X-File-Hash"] = row.sha256
    return response


async def _set_file_archived(request: Request, session: AsyncSession, auth: AuthContext, guide_id: int,
                             file_id: int, archived: bool, version: PublishedVersion) -> GuideFileResponse:
    """Archive or restore a file of a live guide with its audit row, one transaction: the guide row locked first,
    then the file row. A guide the caller does not see: 404 guide_not_found. Archive of an archived file: 404;
    restore of a live one: 409."""
    guide = await _guide_or_404(session, guide_id, auth, lock=True)
    row = await _file_or_404(session, guide_id, file_id, include_deleted=True, lock=True)
    if archived and row.is_deleted:
        raise ApiError(ErrorCode.GUIDE_FILE_NOT_FOUND, file_id=file_id)
    if not archived and not row.is_deleted:
        raise ApiError(ErrorCode.GUIDE_FILE_NOT_ARCHIVED)
    old_values = row.model_dump()
    row.is_deleted = archived
    session.add(row)
    await create_audit_log(
        session, AuditEvent.USER_GUIDE_FILE_ARCHIVE if archived else AuditEvent.USER_GUIDE_FILE_RESTORE,
        params=_file_params(guide, row),
        resource_id=row.id,
        user_id=auth.user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        **client_info(request, auth),
    )
    publish_dictionary_changed(session, Dictionary.GUIDES)
    await session.commit()
    await session.refresh(row)
    return _file_response(row, version)


@router.post(
    GuideRoutes.FILE_ARCHIVE.value,
    response_model=GuideFileResponse,
    responses=problems(ErrorCode.GUIDE_NOT_FOUND, ErrorCode.GUIDE_FILE_NOT_FOUND, *GUARDED),
)
async def archive_guide_file(
        request: Request,
        guide_id: int,
        file_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_ARCHIVE])),
        version: PublishedVersion = Depends(request_version),
):
    """Archive a file of a guide (is_deleted=true): it stays on disk, is left out of the guide's files and its
    download answers 404, unless include_deleted. Undo with POST .../restore. Already archived or unknown: 404; a
    file of an archived guide, or of a guide the caller does not see: 404 guide_not_found."""
    return await _set_file_archived(request, session, auth, guide_id, file_id, archived=True, version=version)


@router.post(
    GuideRoutes.FILE_RESTORE.value,
    response_model=GuideFileResponse,
    responses=problems(ErrorCode.GUIDE_NOT_FOUND, ErrorCode.GUIDE_FILE_NOT_FOUND, ErrorCode.GUIDE_FILE_NOT_ARCHIVED,
                       *GUARDED),
)
async def restore_guide_file(
        request: Request,
        guide_id: int,
        file_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.GUIDE_ARCHIVE])),
        version: PublishedVersion = Depends(request_version),
):
    """Restore an archived file of a guide (is_deleted=false). Not archived: 409. A file of an archived guide, or of
    a guide the caller does not see: 404 guide_not_found."""
    return await _set_file_archived(request, session, auth, guide_id, file_id, archived=False, version=version)
