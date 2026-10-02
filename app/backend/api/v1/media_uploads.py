"""Resumable uploads: POST /media/uploads and the session routes under it.

Built for bad connections. The client opens a session, sends the file in
chunks at explicit offsets, asks the server for the current offset after any
failure and continues from there, then completes. See UPLOADS.md for the
client-facing contract.

Permissions match the one-shot upload routes: media.view + media.create and
write access to the observation's post. A session belongs to the user who
opened it; media.manage_any may operate on any session.
"""
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response, status as fapi_status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.general import Permissions
from app.backend.enums.routes import MediaRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
from app.backend.models.upload_session import UploadSession
from app.backend.api.shared.helpers.media_helper import storage_root
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.api.shared.helpers.upload_helper import (
    check_declared_format, finish_upload, load_observation_for_upload,
    format_rejected, store_rejected, too_large, validation_config_for,
)
from app.backend.api.shared.models.request.media_request import UploadSessionCreate
from app.backend.api.shared.models.response.media_response import UploadResultResponse, UploadSessionResponse
from app.backend.services.access_control import AccessControlService
from app.backend.services.access_errors import AccessErrorCode, access_denied
from app.backend.services.auth_context import AuthContext
from app.backend.services.media import formats, uploads
from app.backend.services.media.formats import FileTooLarge, UnsupportedMediaFormat
from app.backend.services.media.manager import get_media_manager
from app.backend.services.permissions import Require, require
from app.backend.utils.logging_config import get_api_logger
from app.backend.services.api_errors import ApiError, CodedError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.api.shared.models.response.openapi_docs import UPLOAD_SESSION_HEADERS, empty_response, header, with_headers
from app.backend.openapi_problems import GUARDED, problems

router = APIRouter(prefix=MediaRoutes.BASE.value, tags=[MediaRoutes.tag()])
_logger = get_api_logger()

UPLOAD_REQUIRE = require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_CREATE])
CHUNK_CONTENT_TYPES = {"application/offset+octet-stream", "application/octet-stream"}


def _session_headers(row: UploadSession) -> dict:
    return {
        "Upload-Offset": str(int(row.received_bytes)),
        "Upload-Length": str(int(row.expected_size)),
        "Upload-Expires": row.expires_at.isoformat() if row.expires_at else "",
        "Upload-Status": row.status,
        "Cache-Control": "no-store",
    }


def _to_response(row: UploadSession, version: PublishedVersion) -> UploadSessionResponse:
    """The URLs point to the API version the client called"""
    session_url = version.path(MediaRoutes.UPLOAD_SESSION, upload_id=row.id)
    return UploadSessionResponse(
        id=row.id,
        status=row.status,
        observation_id=row.observation_id,
        media_type=row.media_type,
        original_filename=row.original_filename,
        expected_size=int(row.expected_size),
        received_bytes=int(row.received_bytes),
        remaining_bytes=row.remaining_bytes,
        max_chunk_bytes=settings.storage.upload.max_chunk_bytes,
        media_file_id=row.media_file_id,
        error=row.error,
        created_at=row.created_at,
        last_activity_at=row.last_activity_at,
        expires_at=row.expires_at,
        completed_at=row.completed_at,
        urls={"status": session_url, "chunk": session_url,
              "complete": version.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=row.id),
              "abort": version.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=row.id)},
    )


async def _owned_session(session: AsyncSession, auth: AuthContext, upload_id: str) -> UploadSession:
    """The session, visible to its creator or media.manage_any, with post write access still valid"""
    row = await uploads.get_session_row(session, upload_id)
    if row is None:
        raise ApiError(ErrorCode.UPLOAD_SESSION_NOT_FOUND)
    if row.user_id != auth.user.id and not auth.has(Permissions.MEDIA_MANAGE_ANY):
        raise access_denied(
            AccessErrorCode.UPLOAD_SESSION_NOT_OWNER,
            required=Require.of(all=[Permissions.MEDIA_MANAGE_ANY]),
            missing=[Permissions.MEDIA_MANAGE_ANY.value],
        )
    AccessControlService.check_post_write(auth, row.post_id, "media")
    return row


async def _require_open(session: AsyncSession, row: UploadSession) -> UploadSession:
    """410 Gone with the final status when the session can no longer take chunks"""
    if row.status == uploads.STATUS_OPEN and uploads.is_expired(row):
        await uploads.remove_part(row, storage_root())
        await uploads.mark(session, row, uploads.STATUS_EXPIRED, error="session expired before completion")
    if row.status != uploads.STATUS_OPEN:
        # row.error is an internal reason: logged, never returned
        _logger.info(f"upload session {row.id} is {row.status}: {row.error}")
        raise ApiError(
            ErrorCode.UPLOAD_SESSION_CLOSED,
            headers=_session_headers(row),
            session_status=row.status,
            media_file_id=row.media_file_id,
        )
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(MediaRoutes.UPLOADS.value, response_model=List[UploadSessionResponse], responses=problems(*GUARDED))
async def list_upload_sessions(
    status: Optional[str] = Query(None, description="open, completed, aborted or expired; default all"),
    observation_id: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
    version: PublishedVersion = Depends(request_version),
):
    """Your upload sessions. Use it after a page reload to find what to resume.

    media.manage_any lists every user's sessions: administrators (admin.true) over every post,
    everyone else over the posts of their data scope."""
    if auth.has(Permissions.MEDIA_MANAGE_ANY):
        owner = None
        post_ids = None if auth.is_admin else AccessControlService.post_scope(auth)
    else:
        owner = auth.user.id
        post_ids = None
    rows = await uploads.list_sessions(session, owner, status, observation_id, post_ids)
    return [_to_response(row, version) for row in rows]


@router.post(
    MediaRoutes.UPLOADS.value,
    response_model=UploadSessionResponse,
    status_code=fapi_status.HTTP_201_CREATED,
    responses={
        **with_headers(201, "Session open", {**UPLOAD_SESSION_HEADERS, "Location": header("URL of the session: send chunks there")}),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
            ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_FILE_TOO_LARGE,
            ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
            ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN, *GUARDED,
        ),
    },
)
async def create_upload_session(
    request: Request,
    response: Response,
    body: UploadSessionCreate,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
    version: PublishedVersion = Depends(request_version),
):
    """Open a resumable upload. Format, declared Content-Type and size are checked here, before any byte is sent."""
    observation = await load_observation_for_upload(session, auth, body.observation_id)
    registry = await formats.load_registry(session)
    spec = check_declared_format(registry, body.media_type, body.original_filename, body.content_type)
    if body.expected_size > spec.max_file_size_bytes:
        raise too_large(spec, body.expected_size)

    row = uploads.new_session(
        user_id=auth.user.id, observation_id=observation.id, post_id=observation.post_id,
        media_type=spec.media_type, original_filename=body.original_filename, expected_size=body.expected_size,
        declared_content_type=formats.normalise_content_type(body.content_type), expected_sha256=body.sha256,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    await create_audit_log(
        session=session, action="create", resource_type="upload_session", resource_id=None,
        user_id=auth.user.id, new_values={"upload_id": row.id, "expected_size": row.expected_size, "media_type": row.media_type},
        details={"message": f"User {auth.user.username} opened upload session {row.id} for observation #{observation.id} ({body.original_filename}, {body.expected_size} bytes)"},
        **client_info(request, auth),
    )
    await session.commit()

    response.headers.update(_session_headers(row))
    response.headers["Location"] = version.path(MediaRoutes.UPLOAD_SESSION, upload_id=row.id)
    return _to_response(row, version)


@router.get(
    MediaRoutes.UPLOAD_SESSION.value,
    response_model=UploadSessionResponse,
    responses={
        **with_headers(200, "State of the session", UPLOAD_SESSION_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
            AccessErrorCode.UPLOAD_SESSION_NOT_OWNER, ErrorCode.UPLOAD_SESSION_NOT_FOUND, *GUARDED,
        ),
    },
)
async def get_upload_session(
    upload_id: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
    version: PublishedVersion = Depends(request_version),
):
    """Current offset and status. Ask this after every failed chunk and continue from received_bytes."""
    row = await _owned_session(session, auth, upload_id)
    if row.status == uploads.STATUS_OPEN and uploads.is_expired(row):
        await uploads.remove_part(row, storage_root())
        await uploads.mark(session, row, uploads.STATUS_EXPIRED, error="session expired before completion")
    response.headers.update(_session_headers(row))
    return _to_response(row, version)


@router.head(MediaRoutes.UPLOAD_SESSION.value, include_in_schema=False)
async def head_upload_session(
    upload_id: str,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
):
    """Same as GET but headers only (tus style): Upload-Offset, Upload-Length, Upload-Status."""
    row = await _owned_session(session, auth, upload_id)
    return Response(status_code=200, headers=_session_headers(row))


@router.patch(
    MediaRoutes.UPLOAD_SESSION.value,
    status_code=fapi_status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        **empty_response(204, "Chunk stored; Upload-Offset is the new offset", UPLOAD_SESSION_HEADERS),
        **problems(
            ErrorCode.UPLOAD_CHUNK_EMPTY, ErrorCode.UPLOAD_CHUNK_NOT_STORED, ErrorCode.UPLOAD_OFFSET_REQUIRED,
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
            AccessErrorCode.UPLOAD_SESSION_NOT_OWNER, ErrorCode.UPLOAD_SESSION_NOT_FOUND,
            ErrorCode.UPLOAD_OFFSET_MISMATCH, ErrorCode.UPLOAD_WRITER_BUSY, ErrorCode.UPLOAD_SESSION_CLOSED,
            ErrorCode.UPLOAD_CHUNK_OVERRUN, ErrorCode.UPLOAD_CHUNK_TOO_LARGE, ErrorCode.UPLOAD_CHUNK_CONTENT_TYPE,
            *GUARDED,
        ),
    },
)
async def append_upload_chunk(
    request: Request,
    upload_id: str,
    upload_offset: Optional[str] = Header(None, description="Bytes the server already has: must equal received_bytes of the session"),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
):
    """
    Append one chunk. Headers: Upload-Offset (must equal the session's received_bytes),
    Content-Type application/offset+octet-stream, Content-Length. Body: raw bytes.
    204 with the new Upload-Offset; 409 with the real Upload-Offset when they disagree;
    413 when the chunk is larger than max_chunk_bytes or overruns the declared size; 410 when the session is not open.
    """
    row = await _require_open(session, await _owned_session(session, auth, upload_id))

    raw_offset = upload_offset
    if raw_offset is None or not raw_offset.isdigit():
        raise ApiError(ErrorCode.UPLOAD_OFFSET_REQUIRED, headers=_session_headers(row))
    offset = int(raw_offset)
    content_type = formats.normalise_content_type(request.headers.get("content-type"))
    if content_type and content_type not in CHUNK_CONTENT_TYPES:
        raise ApiError(ErrorCode.UPLOAD_CHUNK_CONTENT_TYPE, headers=_session_headers(row))
    max_chunk = settings.storage.upload.max_chunk_bytes
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit():
        length = int(declared_length)
        if length == 0:
            raise ApiError(ErrorCode.UPLOAD_CHUNK_EMPTY, headers=_session_headers(row))
        if length > max_chunk:
            raise ApiError(ErrorCode.UPLOAD_CHUNK_TOO_LARGE, headers=_session_headers(row), max_chunk_bytes=max_chunk, offset=offset)
        if offset + length > int(row.expected_size):
            raise ApiError(ErrorCode.UPLOAD_CHUNK_OVERRUN, headers=_session_headers(row), expected_size=int(row.expected_size), offset=offset)

    try:
        token = await uploads.claim_writer(session, row.id, offset)
    except uploads.OffsetMismatch as exc:
        raise ApiError(
            ErrorCode.UPLOAD_OFFSET_MISMATCH,
            headers={**_session_headers(row), "Upload-Offset": str(exc.current_offset)},
            offset=exc.current_offset,
            sent_offset=offset,
        )
    except uploads.WriterBusy:
        raise ApiError(ErrorCode.UPLOAD_WRITER_BUSY, headers=_session_headers(row), offset=offset)
    except LookupError:
        row = await uploads.get_session_row(session, upload_id)
        await _require_open(session, row)
        raise

    try:
        new_offset = await uploads.append_chunk(session, row, token, storage_root(), request.stream(), offset, max_chunk)
    except uploads.ChunkTooLarge as exc:
        raise exc.to_api_error(headers=_session_headers(row), offset=offset)
    except Exception as exc:
        # client went away mid-chunk or disk error: the part file is truncated back to `offset`
        _logger.warning(f"upload session {upload_id}: chunk at {offset} failed: {exc}")
        raise ApiError(ErrorCode.UPLOAD_CHUNK_NOT_STORED, headers=_session_headers(row), offset=offset)

    row = await uploads.get_session_row(session, upload_id)
    return Response(status_code=fapi_status.HTTP_204_NO_CONTENT, headers={**_session_headers(row), "Upload-Offset": str(new_offset)})


@router.post(
    MediaRoutes.UPLOAD_SESSION_COMPLETE.value,
    response_model=UploadResultResponse,
    responses=problems(
        ErrorCode.MEDIA_CONTENT_INVALID, ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING,
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.UPLOAD_SESSION_NOT_OWNER, ErrorCode.UPLOAD_SESSION_NOT_FOUND, ErrorCode.UPLOAD_INCOMPLETE,
        ErrorCode.UPLOAD_BYTES_MISSING, ErrorCode.UPLOAD_SESSION_CLOSED, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN,
        ErrorCode.UPLOAD_CHECKSUM_MISMATCH, ErrorCode.MEDIA_STORE_FAILED, ErrorCode.UPLOAD_COMPLETE_FAILED,
        ErrorCode.MEDIA_UPLOADS_BUSY, *GUARDED,
    ),
)
async def complete_upload_session(
    request: Request,
    upload_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
):
    """
    Turn a fully received session into a media file: size, optional sha256, format and content checks,
    then the same storage path as the other uploads. Calling it again on a completed session returns the same
    media file (200). 409 when bytes are missing (resend from Upload-Offset); 422 on hash mismatch (session aborted);
    415 / 413 as for other uploads (session aborted).
    """
    row = await _owned_session(session, auth, upload_id)
    if row.status == uploads.STATUS_COMPLETED and row.media_file_id:
        return await finish_upload(request, session, auth, row.media_file_id, row.observation_id, row.original_filename, background_tasks, "resumable, already completed", upload_id=row.id)
    row = await _require_open(session, row)
    root = storage_root()

    if int(row.received_bytes) != int(row.expected_size):
        raise ApiError(
            ErrorCode.UPLOAD_INCOMPLETE,
            headers=_session_headers(row),
            missing_bytes=int(row.remaining_bytes),
            offset=int(row.received_bytes),
            expected_size=int(row.expected_size),
        )
    part = uploads.resolve_part(row, root)
    if not part.exists() or part.stat().st_size != int(row.expected_size):
        await uploads.remove_part(row, root)
        await uploads.mark(session, row, uploads.STATUS_ABORTED, error="part file missing or inconsistent")
        raise ApiError(ErrorCode.UPLOAD_BYTES_MISSING, headers=_session_headers(row), session_status=row.status)

    if row.expected_sha256:
        actual = await uploads.sha256_of(part)
        if actual != row.expected_sha256:
            await uploads.remove_part(row, root)
            await uploads.mark(session, row, uploads.STATUS_ABORTED, error="sha256 mismatch")
            raise ApiError(ErrorCode.UPLOAD_CHECKSUM_MISMATCH, headers=_session_headers(row),
                           expected_sha256=row.expected_sha256, actual_sha256=actual)

    registry = await formats.load_registry(session)
    spec = check_declared_format(registry, row.media_type, row.original_filename, row.declared_content_type)
    try:
        media_file_id = await get_media_manager().store_file_from_path(
            session=session, observation_id=row.observation_id, source_path=str(part),
            original_filename=row.original_filename, media_type=spec.media_type, user_id=row.user_id,
            validation_config=validation_config_for(registry, spec),
        )
    except FileTooLarge as e:
        await uploads.remove_part(row, root)
        await uploads.mark(session, row, uploads.STATUS_ABORTED, error=str(e))
        raise too_large(spec, e.actual_bytes)
    except UnsupportedMediaFormat as e:
        await uploads.remove_part(row, root)
        await uploads.mark(session, row, uploads.STATUS_ABORTED, error=str(e))
        raise format_rejected(registry, e)
    except CodedError as e:
        # Busy is the server's state, not the file's: keep the bytes so complete can be retried
        if e.code != ErrorCode.MEDIA_UPLOADS_BUSY:
            await uploads.remove_part(row, root)
            await uploads.mark(session, row, uploads.STATUS_ABORTED, error=str(e))
        raise store_rejected(e, headers=_session_headers(row))
    except Exception as e:
        _logger.exception(f"upload session {upload_id}: completion failed: {e}")
        raise ApiError(ErrorCode.UPLOAD_COMPLETE_FAILED)

    await uploads.mark(session, row, uploads.STATUS_COMPLETED, media_file_id=media_file_id)
    return await finish_upload(request, session, auth, media_file_id, row.observation_id, row.original_filename, background_tasks, "resumable", upload_id=row.id)


@router.post(
    MediaRoutes.UPLOAD_SESSION_ABORT.value,
    response_model=UploadSessionResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.UPLOAD_SESSION_NOT_OWNER, ErrorCode.UPLOAD_SESSION_NOT_FOUND,
        ErrorCode.UPLOAD_SESSION_COMPLETED, *GUARDED,
    ),
)
async def abort_upload_session(
    request: Request,
    upload_id: str,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
    version: PublishedVersion = Depends(request_version),
):
    """Give up on an open session and delete the received bytes. Completed sessions cannot be aborted (409)."""
    row = await _owned_session(session, auth, upload_id)
    if row.status == uploads.STATUS_COMPLETED:
        raise ApiError(ErrorCode.UPLOAD_SESSION_COMPLETED)
    if row.status == uploads.STATUS_OPEN:
        await uploads.remove_part(row, storage_root())
        await uploads.mark(session, row, uploads.STATUS_ABORTED, error="aborted by client")
        await create_audit_log(
            session=session, action="abort", resource_type="upload_session", resource_id=None,
            user_id=auth.user.id, details={"message": f"User {auth.user.username} aborted upload session {row.id} at {row.received_bytes}/{row.expected_size} bytes", "upload_id": row.id},
            **client_info(request, auth),
        )
        await session.commit()
    return _to_response(row, version)
