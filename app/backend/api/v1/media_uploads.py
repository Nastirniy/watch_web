"""Resumable uploads: POST /media/uploads and the session routes under it.

Built for bad connections. The client opens a session, sends the file in
chunks at explicit offsets, asks the server for the current offset after any
failure and continues from there, then completes. See UPLOADS.md for the
client-facing contract.

Permissions match the one-shot upload routes: media.view + media.create in
effect at the observation's post. A session belongs to the user who opened it;
media.manage_any may operate on the sessions of the posts where it is in effect.
"""
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response, status as fapi_status
from starlette.requests import ClientDisconnect
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.general import Permissions
from app.backend.enums.media_types import ContentType
from app.backend.enums.routes import MediaRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.auth.scope import Node
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log
from app.backend.models.observation import Observation
from app.backend.models.upload_session import UploadSession
from app.backend.api.shared.helpers.media_helper import storage_root
from app.backend.api.shared.helpers.post_helper import ensure_post_not_archived
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.api.shared.helpers.upload_helper import (
    check_declared_format, load_observation_for_upload, record_upload,
    format_rejected, store_rejected, too_large, validation_config_for,
)
from app.backend.api.v1.models.request.media_uploads import UploadSessionCreate
from app.backend.api.v1.models.response.media_uploads import UPLOAD_SESSION_HEADERS, UploadSessionResponse
from app.backend.api.v1.models.response.media import UploadResultResponse, upload_result
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.access_errors import access_denied
from app.backend.auth.context import AuthContext
from app.backend.services.files import formats
from app.backend.services.media import uploads
from app.backend.services.files.formats import FileTooLarge, UnsupportedMediaFormat
from app.backend.services.media.manager import get_media_manager
from app.backend.auth.require import Require, require
from app.backend.utils.logging_config import get_api_logger
from app.backend.utils.datetime_utils import assume_utc
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import CodedError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.api.shared.openapi_docs import empty_response, header, with_headers
from app.backend.errors.openapi import GUARDED, problems

router = APIRouter(prefix=MediaRoutes.BASE.value, tags=[MediaRoutes.tag()])
_logger = get_api_logger()

UPLOAD_REQUIRE = require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_CREATE])
# The tus protocol's own name for a chunk body, and plain opaque bytes
CHUNK_CONTENT_TYPES = frozenset({ContentType.OFFSET_OCTET_STREAM, ContentType.OCTET_STREAM})


def _session_headers(row: UploadSession) -> dict:
    return {
        "Upload-Offset": str(int(row.received_bytes)),
        "Upload-Length": str(int(row.expected_size)),
        "Upload-Expires": assume_utc(row.expires_at).isoformat() if row.expires_at else "",
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
        error=uploads.error_code(row),
        created_at=row.created_at,
        last_activity_at=row.last_activity_at,
        expires_at=row.expires_at,
        completed_at=row.completed_at,
        urls={"status": session_url, "chunk": session_url,
              "complete": version.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=row.id),
              "abort": version.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=row.id)},
    )


async def _owned_session(session: AsyncSession, auth: AuthContext, upload_id: str, lock: bool = False) -> UploadSession:
    """The session, visible to its creator or to media.manage_any in effect at its post, while media.view and
    media.create are still in effect there; lock=True for the routes that change it (uploads.get_session_row)"""
    row = await uploads.get_session_row(session, upload_id, lock=lock)
    if row is None:
        raise ApiError(ErrorCode.UPLOAD_SESSION_NOT_FOUND)
    if row.user_id != auth.user.id and not auth.has_at_post(Permissions.MEDIA_MANAGE_ANY, row.post_id):
        raise access_denied(
            AccessErrorCode.UPLOAD_SESSION_NOT_OWNER,
            required=Require.of(all=[Permissions.MEDIA_MANAGE_ANY]),
            missing=[Permissions.MEDIA_MANAGE_ANY.value],
        )
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, row.post_id, "media")
    AccessControlService.check_post(auth, Permissions.MEDIA_CREATE, row.post_id, "media")
    return row


async def _expire_if_due(session: AsyncSession, row: UploadSession) -> UploadSession:
    """The row after the expiry check every session route applies (GET, HEAD, chunk, complete)"""
    if await uploads.expire_if_due(session, row, storage_root()):
        row = await uploads.get_session_row(session, row.id)
    return row


def _completing(row: UploadSession) -> ApiError:
    return ApiError(ErrorCode.UPLOAD_SESSION_COMPLETING, headers=_session_headers(row))


def _closed(row: UploadSession) -> ApiError:
    _logger.info(f"upload session {row.id} is {row.status} ({row.error})")
    return ApiError(
        ErrorCode.UPLOAD_SESSION_CLOSED,
        headers=_session_headers(row),
        session_status=row.status,
        media_file_id=row.media_file_id,
    )


def _incomplete(row: UploadSession) -> ApiError:
    return ApiError(
        ErrorCode.UPLOAD_INCOMPLETE,
        headers=_session_headers(row),
        missing_bytes=int(row.remaining_bytes),
        offset=int(row.received_bytes),
        expected_size=int(row.expected_size),
    )


async def _refuse_archived_observation(session: AsyncSession, row: UploadSession,
                                      archived: Optional[bool] = None) -> None:
    """404 observation_not_found when the observation of an open session is archived (an archive cancels the
    sessions of what it archives: one archived without that, before this rule or by a restored backup, is refused
    here). archived: the flag of the locked row, read here otherwise."""
    if archived is None:
        archived = (await session.exec(
            select(Observation.is_deleted).where(Observation.id == row.observation_id))).first()
    if archived is None or archived:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND, headers=_session_headers(row))


async def _require_open(session: AsyncSession, row: UploadSession) -> UploadSession:
    """409 while a complete stores the file, 410 Gone with the final status when the session can no longer
    take chunks"""
    row = await _expire_if_due(session, row)
    if row.status == uploads.STATUS_COMPLETING:
        raise _completing(row)
    if row.status != uploads.STATUS_OPEN:
        raise _closed(row)
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(MediaRoutes.UPLOADS.value, response_model=List[UploadSessionResponse], responses=problems(*GUARDED))
async def list_upload_sessions(
    status: Optional[str] = Query(None, description="open, completing, completed, aborted or expired; default all"),
    observation_id: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
    version: PublishedVersion = Depends(request_version),
):
    """Your upload sessions. Use it after a page reload to find what to resume.

    With media.manage_any, other users' sessions too, at the posts where media.manage_any is in effect."""
    others_post_ids = AccessControlService.scope_for(auth, Permissions.MEDIA_MANAGE_ANY)
    rows = await uploads.list_sessions(session, auth.user.id, status, observation_id, others_post_ids)
    return [_to_response(row, version) for row in rows]


@router.post(
    MediaRoutes.UPLOADS.value,
    response_model=UploadSessionResponse,
    status_code=fapi_status.HTTP_201_CREATED,
    responses={
        **with_headers(201, "Session open", {**UPLOAD_SESSION_HEADERS, "Location": header("URL of the session: send chunks there")}),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
            ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.MEDIA_FILE_TOO_LARGE,
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
    """Open a resumable upload. Format, declared Content-Type and size are checked here, before any byte is sent.
    An unknown or archived observation: 404 observation_not_found; an observation of an archived post: 409
    post_archived. Archiving the observation, its post or a unit with its posts later cancels the session (status
    aborted, see UPLOADS.md)."""
    # The write begins with the post and the observation (shared): an archive waits for it or is seen
    observation = await load_observation_for_upload(session, auth, body.observation_id, lock_parents=True)
    registry = await formats.observation_registry(session)
    spec = check_declared_format(registry, body.media_type, body.original_filename, body.content_type)
    if body.expected_size > spec.effective_limit_bytes:
        raise too_large(spec, body.expected_size)

    # A declared name of the format is kept as its canonical name (an alias as the format's name); an opaque one as is
    declared = formats.normalise_content_type(body.content_type)
    row = uploads.new_session(
        user_id=auth.user.id, observation_id=observation.id, post_id=observation.post_id,
        media_type=spec.media_type, original_filename=body.original_filename, expected_size=body.expected_size,
        declared_content_type=spec.content_type if declared in spec.all_mimes else declared,
        expected_sha256=body.sha256,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    # The session and its audit row in one transaction
    await create_audit_log(
        session, AuditEvent.UPLOAD_SESSION_CREATE,
        params={"upload_id": row.id, "media_type": row.media_type, "observation_id": row.observation_id,
                "file_name": row.original_filename},
        scope=Node.post(row.post_id), user_id=auth.user.id, new_values={"upload_id": row.id, "expected_size": row.expected_size, "media_type": row.media_type},
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
    row = await _expire_if_due(session, await _owned_session(session, auth, upload_id))
    response.headers.update(_session_headers(row))
    return _to_response(row, version)


@router.head(MediaRoutes.UPLOAD_SESSION.value, include_in_schema=False)
async def head_upload_session(
    upload_id: str,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
):
    """Same as GET but headers only (tus style): Upload-Offset, Upload-Length, Upload-Status."""
    row = await _expire_if_due(session, await _owned_session(session, auth, upload_id))
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
            ErrorCode.UPLOAD_SESSION_COMPLETING, ErrorCode.UPLOAD_BYTES_MISSING,
            ErrorCode.UPLOAD_CHUNK_OVERRUN, ErrorCode.UPLOAD_CHUNK_TOO_LARGE, ErrorCode.UPLOAD_CHUNK_CONTENT_TYPE,
            ErrorCode.OBSERVATION_NOT_FOUND, *GUARDED,
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
    413 when the chunk is larger than max_chunk_bytes or overruns the declared size; 410 when the session is not open
    (or the stored bytes are gone: the session is aborted; an archive of its observation, post or unit cancels it);
    409 while a complete stores the file; 404 observation_not_found when its observation is archived.
    """
    row = await _require_open(session, await _owned_session(session, auth, upload_id, lock=True))
    await _refuse_archived_observation(session, row)

    raw_offset = upload_offset
    if raw_offset is None or not raw_offset.isdigit():
        raise ApiError(ErrorCode.UPLOAD_OFFSET_REQUIRED, headers=_session_headers(row))
    offset = int(raw_offset)
    raw_type = (request.headers.get("content-type") or "").strip()
    content_type = formats.normalise_content_type(raw_type)
    if raw_type and content_type not in CHUNK_CONTENT_TYPES:
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
        await _require_open(session, await uploads.get_session_row(session, upload_id))
        raise

    try:
        new_offset = await uploads.append_chunk(session, row, token, storage_root(), request.stream(), offset, max_chunk)
    except uploads.ChunkTooLarge as exc:
        # append_chunk rolled the session back (which expires the row) and truncated the file to `offset`
        row = await uploads.get_session_row(session, upload_id)
        raise ApiError.from_coded(exc, headers=_session_headers(row), offset=offset)
    except uploads.BytesMissing as exc:
        _logger.warning(f"upload session {upload_id}: the part file holds fewer than the {offset} bytes received; session aborted")
        raise ApiError.from_coded(exc, headers=_session_headers(await uploads.get_session_row(session, upload_id)))
    except uploads.WriterLost:
        # The request stalled past the writer's stale time and another one took the session (or it ended):
        # this chunk is not counted and the part file was left to the other request
        _logger.warning(f"upload session {upload_id}: chunk at {offset} not stored, the writer lost the session")
        row = await _require_open(session, await uploads.get_session_row(session, upload_id))
        raise ApiError(ErrorCode.UPLOAD_CHUNK_NOT_STORED, headers=_session_headers(row), offset=int(row.received_bytes))
    except (ClientDisconnect, OSError) as exc:
        # The transfer broke (client gone) or the disk refused the bytes: append_chunk truncated the part
        # file back to `offset` (when it still held the session), so the client resends from the session's
        # offset. Anything else is a bug: a plain 500.
        _logger.warning(f"upload session {upload_id}: chunk at {offset} not stored: {exc!r}")
        row = await uploads.get_session_row(session, upload_id)
        raise ApiError(ErrorCode.UPLOAD_CHUNK_NOT_STORED, headers=_session_headers(row), offset=int(row.received_bytes))

    row = await uploads.get_session_row(session, upload_id)
    return Response(status_code=fapi_status.HTTP_204_NO_CONTENT, headers={**_session_headers(row), "Upload-Offset": str(new_offset)})


@router.post(
    MediaRoutes.UPLOAD_SESSION_COMPLETE.value,
    response_model=UploadResultResponse,
    responses=problems(
        ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING,
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.UPLOAD_SESSION_NOT_OWNER, ErrorCode.UPLOAD_SESSION_NOT_FOUND, ErrorCode.UPLOAD_INCOMPLETE,
        ErrorCode.UPLOAD_BYTES_MISSING, ErrorCode.UPLOAD_SESSION_CLOSED, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN,
        ErrorCode.UPLOAD_CHECKSUM_MISMATCH, ErrorCode.MEDIA_STORE_FAILED, ErrorCode.POST_ARCHIVED,
        ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_UPLOADS_BUSY, ErrorCode.UPLOAD_SESSION_COMPLETING,
        ErrorCode.UPLOAD_WRITER_BUSY, *GUARDED,
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
    media file (200). 409 when bytes are missing (resend from Upload-Offset) or while another complete stores the
    file; 422 on hash mismatch (session aborted); 415 / 413 as for other uploads (session aborted). An archive of the
    observation, its post or a unit with its posts cancels the session: 410 upload_session_closed (aborted). An
    archived observation: 404 observation_not_found; the post archived: 409 post_archived (the session stays open
    when nothing cancelled it). When the archive comes while the file is being stored, the store refuses it (404
    observation_not_found or 409 post_archived) and the session ends aborted.
    """
    # Ownership and scope on the session as read, then the write begins with the observation row (shared): an archive
    # of the observation waits for the claim or is seen; then the session row
    row = await _owned_session(session, auth, upload_id)
    observation = await uploads.lock_observation(session, row.observation_id)
    row = await _expire_if_due(session, await uploads.get_session_row(session, upload_id, lock=True, first=False))
    if row.status == uploads.STATUS_COMPLETED and row.media_file_id:
        return await _completed_result(session, row)
    if row.status not in (uploads.STATUS_OPEN, uploads.STATUS_COMPLETING):
        raise _closed(row)
    # A completed session above answers its file again (no write); nothing new goes into an archived observation or
    # post
    await _refuse_archived_observation(session, row, observation.is_deleted if observation is not None else None)
    await ensure_post_not_archived(session, row.post_id)
    if int(row.received_bytes) != int(row.expected_size):
        raise _incomplete(row)

    # One complete at a time: the claim moves the session to `completing` before anything is read
    token = await uploads.claim_completion(session, row.id)
    if token is None:
        return await _claim_refused(session, row.id)
    row = await uploads.get_session_row(session, row.id)
    try:
        media_file_id = await _store_claimed(session, row, token)
    except BaseException:
        # Not the file's fault (a refused file aborted the session already): open again, complete can be retried.
        # The failure may have come from the request session (a failed flush or commit, a lost connection), which
        # takes no statement before a rollback
        await session.rollback()
        await uploads.release_completion(session, upload_id, token)
        raise

    media_file = await record_upload(request, session, auth, media_file_id, row.observation_id, row.post_id, row.original_filename, background_tasks, "resumable", upload_id=row.id)
    return upload_result(media_file, "resumable", upload_id=row.id)


async def _completed_result(session: AsyncSession, row: UploadSession) -> UploadResultResponse:
    """A completed session answers its media file again: nothing is stored, probed or audited"""
    media_file = await uploads.completed_media(session, row)
    return upload_result(media_file, "resumable, already completed", upload_id=row.id)


async def _claim_refused(session: AsyncSession, upload_id: str) -> UploadResultResponse:
    """Another request holds the session: answer by what it is now"""
    row = await uploads.get_session_row(session, upload_id)
    if row.status == uploads.STATUS_COMPLETED and row.media_file_id:
        return await _completed_result(session, row)
    if row.status == uploads.STATUS_COMPLETING:
        raise _completing(row)
    if row.status != uploads.STATUS_OPEN:
        raise _closed(row)
    if int(row.received_bytes) != int(row.expected_size):
        raise _incomplete(row)
    raise ApiError(ErrorCode.UPLOAD_WRITER_BUSY, headers=_session_headers(row), offset=int(row.received_bytes))


async def _abort_claimed(session: AsyncSession, row: UploadSession, token: str, code: ErrorCode, reason: str) -> UploadSession:
    """End a claimed session as aborted by `code`: the code is stored, the internal reason only logged"""
    _logger.warning(f"upload session {row.id} aborted at completion ({code.value}): {reason}")
    await uploads.abort_session(session, row, storage_root(), code, token=token)
    return await uploads.get_session_row(session, row.id)


async def _rolled_back(session: AsyncSession, upload_id: str) -> UploadSession:
    """The session row after a failed store: the request session is rolled back first (the manager used it, and a
    failed flush or commit takes no statement before a rollback), which expires the row, so it is read again"""
    await session.rollback()
    return await uploads.get_session_row(session, upload_id)


async def _store_claimed(session: AsyncSession, row: UploadSession, token: str) -> int:
    """Check and store the part file of a session this request claimed; the media file id"""
    upload_id = row.id
    part = uploads.resolve_part(row, storage_root())
    if not part.exists() or part.stat().st_size != int(row.expected_size):
        row = await _abort_claimed(session, row, token, ErrorCode.UPLOAD_BYTES_MISSING, "part file missing or inconsistent")
        raise ApiError(ErrorCode.UPLOAD_BYTES_MISSING, headers=_session_headers(row), session_status=row.status)

    if row.expected_sha256:
        actual = await uploads.sha256_of(part)
        if actual != row.expected_sha256:
            row = await _abort_claimed(session, row, token, ErrorCode.UPLOAD_CHECKSUM_MISMATCH, "sha256 mismatch")
            raise ApiError(ErrorCode.UPLOAD_CHECKSUM_MISMATCH, headers=_session_headers(row),
                           expected_sha256=row.expected_sha256, actual_sha256=actual)

    registry = await formats.observation_registry(session)
    spec = check_declared_format(registry, row.media_type, row.original_filename, row.declared_content_type)
    try:
        media_file_id = await get_media_manager().store_file_from_path(
            session=session, observation_id=row.observation_id, source_path=str(part),
            original_filename=row.original_filename, media_type=spec.media_type, user_id=row.user_id,
            validation_config=validation_config_for(spec),
        )
    except FileTooLarge as e:
        await _abort_claimed(session, await _rolled_back(session, upload_id), token, ErrorCode.MEDIA_FILE_TOO_LARGE, str(e))
        raise too_large(spec, e.actual_bytes)
    except UnsupportedMediaFormat as e:
        await _abort_claimed(session, await _rolled_back(session, upload_id), token, e.code, str(e))
        raise format_rejected(registry, e)
    except CodedError as e:
        row = await _rolled_back(session, upload_id)
        if e.code == ErrorCode.MEDIA_UPLOADS_BUSY:
            # Busy is the server's state, not the file's: keep the bytes so complete can be retried
            await uploads.release_completion(session, upload_id, token)
            row = await uploads.get_session_row(session, upload_id)
        else:
            row = await _abort_claimed(session, row, token, e.code, str(e))
        raise store_rejected(e, headers=_session_headers(row))

    if not await uploads.finish_completion(session, upload_id, token, media_file_id):
        # Only after COMPLETION_STALE_SECONDS: another complete took the session over meanwhile
        _logger.warning(f"upload session {upload_id}: stored as media {media_file_id} after its completion claim went stale")
    return media_file_id


@router.post(
    MediaRoutes.UPLOAD_SESSION_ABORT.value,
    response_model=UploadSessionResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.UPLOAD_SESSION_NOT_OWNER, ErrorCode.UPLOAD_SESSION_NOT_FOUND,
        ErrorCode.UPLOAD_SESSION_COMPLETED, ErrorCode.UPLOAD_SESSION_COMPLETING, *GUARDED,
    ),
)
async def abort_upload_session(
    request: Request,
    upload_id: str,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(UPLOAD_REQUIRE),
    version: PublishedVersion = Depends(request_version),
):
    """Give up on an open session and delete the received bytes. Completed sessions cannot be aborted (409), nor a
    session while a complete stores its file (409)."""
    row = await _owned_session(session, auth, upload_id, lock=True)
    if row.status in (uploads.STATUS_OPEN, uploads.STATUS_COMPLETING):
        async def audit() -> None:
            await create_audit_log(
                session, AuditEvent.UPLOAD_SESSION_ABORT,
                params={"upload_id": row.id, "media_type": row.media_type, "observation_id": row.observation_id,
                        "file_name": row.original_filename},
                scope=Node.post(row.post_id), user_id=auth.user.id,
                new_values={"received_bytes": row.received_bytes, "expected_size": row.expected_size},
                **client_info(request, auth),
            )

        # The abort and its audit row in one transaction; the part file is removed after it committed
        await uploads.abort_session(session, row, storage_root(), record=audit)
        row = await uploads.get_session_row(session, upload_id)
    if row.status == uploads.STATUS_COMPLETING:
        raise _completing(row)
    if row.status == uploads.STATUS_COMPLETED:
        raise ApiError(ErrorCode.UPLOAD_SESSION_COMPLETED)
    return _to_response(row, version)
