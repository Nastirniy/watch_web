"""Upload pipeline shared by the upload routes: format checks before the body is read, the 413 / 415
problems, the observation an upload goes to, and what every upload does once the file is stored."""

import asyncio
from typing import Iterable, List, Optional

from fastapi import BackgroundTasks, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.post import Post
from app.backend.models.upload_session import UploadSession
from app.backend.auth.context import AuthContext
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.move_access import committed
from app.backend.services.media_manager import FileValidationConfig
from app.backend.services.files import formats
from app.backend.services.files.names import DANGEROUS_EXTENSIONS, extension_of
from app.backend.services.media import jobs, renderer, uploads, urls as thumb_urls
from app.backend.services.media.probe import has_no_video_stream, metadata_for, resolve_tools
from app.backend.services.move_queue import waiting_at_post
from app.backend.enums.general import TIMED_MEDIA_TYPES, MediaType, Permissions, UploadPurpose, UploadTransport, ThumbnailFormat, ThumbnailSize
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.auth.scope import Node
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log
from app.backend.enums.events import MediaChange
from app.backend.services.events import publish_media_changed
from app.backend.utils.logging_config import get_api_logger
from app.backend.config import settings
from app.backend.api.shared.helpers.media_helper import storage_root
from app.backend.api.shared.helpers.move_helper import move_pending, retry_after, run_due_move, run_released_moves
from app.backend.api.shared.helpers.post_helper import ensure_post_not_archived
from app.backend.enums.errors import ErrorCode
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import CodedError
from app.backend.utils.row_locks import lock, lock_first, start_write

_logger = get_api_logger()


def validation_config_for(spec: formats.FormatSpec) -> FileValidationConfig:
    """Manager-side checks (size, extension, sniffed type) driven by the resolved format; an accepted file is stored
    under the format's canonical content type"""
    return FileValidationConfig(
        max_size=spec.effective_limit_bytes,
        sanitize_filename=True,
        allowed_extensions=set(spec.extensions),
        allowed_mime_types=set(spec.all_mimes),
        content_type=spec.content_type,
    )


def format_rejected(registry: formats.FormatRegistry, exc: formats.UnsupportedMediaFormat) -> ApiError:
    params = {k: v for k, v in exc.params.items() if k != "media_type"}
    return _unsupported(registry, exc.code, exc.media_type, **params)


def store_rejected(exc: CodedError, headers: dict = None) -> ApiError:
    """A file refused by MediaManager.store_file_from_path or services/files/store.store_guide_file; the status comes
    with its problem type"""
    if exc.code is ErrorCode.MOVE_PENDING:
        headers = {**(headers or {}), **retry_after()}
    return ApiError.from_coded(exc, headers=headers)


def too_large(spec: formats.FormatSpec, actual: int = None, limit: Optional[int] = None) -> ApiError:
    """413 of a file of the format: the format's limit, or the limit the store held the file to (FileTooLarge
    limit_bytes: the store reads the registry again)"""
    return ApiError(
        ErrorCode.MEDIA_FILE_TOO_LARGE,
        content_type=spec.content_type,
        max_file_size_bytes=spec.effective_limit_bytes if limit is None else limit,
        file_size_bytes=actual,
    )


def check_declared_format(registry: formats.FormatRegistry, media_type: str, filename: str, content_type: str = None) -> formats.FormatSpec:
    """Reject before touching the body (registry: formats.observation_registry): unknown media_type, extension or declared Content-Type outside the registry"""
    media_type = (media_type or "").lower()
    if media_type not in registry.media_types():
        raise _unsupported(registry, ErrorCode.MEDIA_TYPE_UNKNOWN, media_type)
    spec = registry.resolve(UploadPurpose.OBSERVATION_MEDIA, filename)
    if spec is not None and spec.media_type != media_type:
        spec = None
    if spec is None:
        raise _unsupported(registry, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, media_type, extension=extension_of(filename))
    _check_declared_content_type(registry, spec, content_type)
    return spec


def check_guide_format(registry: formats.FormatRegistry, filename: str, content_type: str = None) -> formats.FormatSpec:
    """The document format of a user guide file, before the body is read (registry: formats.load_registry): a
    dangerous extension, an extension of no document format or a declared Content-Type outside the format is 415
    with Accept-Post (the store checks the bytes again: services/files/store.py)"""
    media_type = MediaType.DOCUMENT.value
    extension = extension_of(filename)
    if extension in DANGEROUS_EXTENSIONS:
        raise _unsupported(registry, ErrorCode.MEDIA_EXTENSION_DANGEROUS, media_type, extension=extension)
    spec = registry.resolve(UploadPurpose.USER_GUIDE, filename)
    if spec is None:
        raise _unsupported(registry, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, media_type, extension=extension)
    _check_declared_content_type(registry, spec, content_type)
    return spec


def _check_declared_content_type(registry: formats.FormatRegistry, spec: formats.FormatSpec,
                                 content_type: Optional[str]) -> None:
    """The Content-Type the client declared for the file: absent or opaque (application/octet-stream) is left to the
    sniffer; another type of the format's media type is a mismatch, any other type not accepted (415)"""
    media_type = spec.media_type
    declared = formats.normalise_content_type(content_type)
    if declared is None and content_type and content_type.strip():
        # malformed (utils/content_type.py): refused like a type outside the registry, the header never echoed
        raise _unsupported(registry, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED, media_type)
    if declared and declared not in formats.OPAQUE_CONTENT_TYPES and declared not in spec.all_mimes:
        if declared in registry.mime_set(media_type):
            raise _unsupported(registry, ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, media_type,
                               content_type=declared, expected_content_type=spec.content_type)
        raise _unsupported(registry, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED, media_type, content_type=declared)


async def load_observation_for_upload(session: AsyncSession, auth: AuthContext, observation_id: int,
                                     lock_parents: bool = False) -> Observation:
    """The observation an upload goes to: 404 when missing or archived, 403 without media.view and media.create in
    effect at its post, 409 post_archived when its post is archived, 409 move_pending while a move waits above it. A
    move that waits for nothing any more runs first (move_helper.run_due_move), and everything here is asked again at
    the post's new place, with the tree of that moment: the rights of the request were read before the move.

    lock_parents for a write that inserts a row of the observation now (an upload session): its post (shared, the
    columns only: the Post entity's presence subqueries would take the snapshot) as the first statement of the
    write, then the observation (shared), as the observation create does (posts before observations). An archive of
    the observation, of the post or of a unit above it waits for the write, or the write sees it archived, and the
    insert never meets a parent row changed after the snapshot (MariaDB error 1020). A move locks the post too: the
    waiting moves read after these locks decide, whichever came first (services/moves.py). Without lock_parents the
    check is an early refusal only: the store transaction of a one-shot upload asks again under its locks
    (MediaManager.store_file_from_path, refuse_waiting_move)."""
    if not lock_parents:
        observation = (await session.exec(
            select(Observation).where(Observation.id == observation_id, Observation.is_deleted == False)
        )).first()
        if not observation:
            raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
        AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
        AccessControlService.check_post(auth, Permissions.MEDIA_CREATE, observation.post_id, "media")
        await ensure_post_not_archived(session, observation.post_id)
    else:
        # An observation never changes its post: read in the request's read transaction, which lock_first ends
        post_id = (await session.exec(select(Observation.post_id).where(Observation.id == observation_id))).first()
        if post_id is None:
            raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
        post = await lock_first(session, select(Post.id, Post.is_deleted).where(Post.id == post_id), shared=True)
        observation = await lock(session, select(Observation).where(Observation.id == observation_id), shared=True)
        if observation is None or observation.is_deleted:
            raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
        AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
        AccessControlService.check_post(auth, Permissions.MEDIA_CREATE, observation.post_id, "media")
        if post is not None and post.is_deleted:
            raise ApiError(ErrorCode.POST_ARCHIVED, post_id=post_id)
    if await _move_ended_above(session, observation.post_id):
        return await load_observation_for_upload(session, await committed(session, auth), observation_id, lock_parents)
    return observation


async def _move_ended_above(session: AsyncSession, post_id: int) -> bool:
    """False: no move waits above the post, read under the caller's locks when it holds them. True: one waited for
    nothing and has ended now, the caller's transaction is over and it asks again. 409 move_pending: one still waits.
    The upload path pays one indexed read; the rest only while a move waits."""
    move_id = await waiting_at_post(session, post_id)
    if move_id is None:
        return False
    if await run_due_move(session, move_id):
        return True
    raise move_pending(move_id)


async def cancel_pending_uploads(request: Request, session: AsyncSession, auth: AuthContext,
                                 rows: Iterable[UploadSession], code: ErrorCode) -> List[UploadSession]:
    """An archive cancels the pending uploads of what it archives (services/media/uploads.py cancel_pending): the
    sessions it locked (uploads.lock_pending) end as aborted by code, each with its upload_session.abort audit row
    (the archiving user, the archive's client), in the archive's transaction. Returns the cancelled sessions: the
    caller removes their part files after its commit (uploads.remove_parts)."""
    async def audit(row: UploadSession) -> None:
        await create_audit_log(
            session, AuditEvent.UPLOAD_SESSION_ABORT,
            params={"upload_id": row.id, "media_type": row.media_type, "observation_id": row.observation_id,
                    "file_name": row.original_filename},
            scope=Node.post(row.post_id), user_id=auth.user.id,
            new_values={"received_bytes": row.received_bytes, "expected_size": row.expected_size,
                        "error": code.value},
            **client_info(request, auth),
        )
    return await uploads.cancel_pending(session, rows, code, record=audit)


async def remove_cancelled_parts(session: AsyncSession, rows: Iterable[UploadSession]) -> None:
    """After the archive's commit: the part files of the sessions cancel_pending_uploads ended, then the moves those
    sessions held"""
    rows = list(rows)
    await uploads.remove_parts(rows, storage_root())
    await run_released_moves(session, {row.post_id for row in rows})


async def record_upload(
    request: Request,
    session: AsyncSession,
    auth: AuthContext,
    media_file_id: int,
    observation_id: int,
    post_id: int,
    original_filename: str,
    background_tasks: BackgroundTasks,
    transport: UploadTransport,
    upload_id: Optional[str] = None,
) -> MediaFile:
    """The last transaction of an upload, once the file is in storage: probed video/audio metadata, the release of
    the poster hold and the audit row, committed together; then the medium thumbnail is pre-warmed.

    The probe runs before that transaction and outside any other: the row is read and the read transaction ended
    first, so no read view lives across ffprobe (seconds for a video), and the write begins with the row lock.
    When this transaction fails (500), the file and its row stay stored (a move cannot be rolled back) without
    metadata and audit row, and the poster job stays held: the hold runs out after jobs.UPLOAD_HOLD and the worker
    takes the poster like any other (it probes the metadata too; `media_tools probe-missing` fills metadata of
    other media types).

    transport: how the file came (the audit param via)."""
    media_file = (await session.exec(select(MediaFile).where(MediaFile.id == media_file_id))).one()
    await start_write(session)
    metadata = None
    if media_file.media_type in TIMED_MEDIA_TYPES:
        metadata = await _probe_headers(media_file)

    # The write begins with the row lock, on the committed copy: a worker may have claimed the poster meanwhile (a
    # hold that ran out, a duplicate whose own upload released it); a row read before the claim is refused on
    # MariaDB, 1020
    media_file = await lock_first(session, select(MediaFile).where(MediaFile.id == media_file_id))
    if metadata is not None:
        for field_name, value in metadata.items():
            setattr(media_file, field_name, value)
    if media_file.is_video:
        await session.flush()
        if metadata and has_no_video_stream(metadata):
            # Only audio in a video upload: no poster or storyboard can exist, the worker never tries
            await jobs.mark_poster_unsupported(session, media_file.id, commit=False)
        # The file is in storage: the worker may take the poster job once this transaction commits
        await jobs.release_poster_hold(session, media_file.id, commit=False)
        await session.refresh(media_file)

    await create_audit_log(
        session, AuditEvent.MEDIA_CREATE,
        params={"observation_id": observation_id, "file_name": original_filename, "media_type": media_file.media_type,
                "via": transport.value, **({"upload_id": upload_id} if upload_id else {})},
        scope=Node.post(post_id), resource_id=media_file_id,
        user_id=auth.user.id, new_values=media_file.model_dump(),
        **client_info(request, auth),
    )
    # One event per stored file (any transport), never per chunk: a duplicate of a stored file names that file again
    publish_media_changed(session, media_file_id, observation_id, post_id, MediaChange.CREATED)
    await session.commit()

    if media_file.is_image and settings.storage.thumbnail.prewarm and settings.storage.enable_nginx:
        warm = thumb_urls.prewarm_url(media_file, ThumbnailSize.MEDIUM.value, ThumbnailFormat.AUTO.value)
        if warm:
            background_tasks.add_task(renderer.prewarm, warm)
    return media_file


def _unsupported(registry: formats.FormatRegistry, code: ErrorCode, media_type: str = None, **params) -> ApiError:
    """415 with an Accept-Post header naming the Content-Types the endpoint accepts"""
    scope = media_type if media_type and media_type in registry.media_types() else None
    return ApiError(
        code,
        headers={"Accept-Post": registry.accept_post_header(scope)},
        media_type=media_type,
        accepted_content_types=registry.content_types(scope),
        accepted_extensions=registry.extensions(scope),
        **params,
    )


async def _probe_headers(media_file: MediaFile) -> dict:
    """ffprobe on the stored file (headers only, bounded); empty on any failure so an upload never fails on metadata"""
    source = media_file.resolve_path(storage_root())
    try:
        ffmpeg, ffprobe = resolve_tools()
        return await asyncio.wait_for(asyncio.to_thread(metadata_for, source, media_file.mime_type, ffmpeg, ffprobe), timeout=30)
    except Exception as exc:
        _logger.warning(f"probe skipped for media {media_file.id}: {exc}")
        return {}
