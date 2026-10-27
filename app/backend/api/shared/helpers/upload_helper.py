"""Upload pipeline shared by the upload routes: format checks before the body is read, the 413 / 415
problems, the observation an upload goes to, and what every upload does once the file is stored."""

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.auth.context import AuthContext
from app.backend.auth.access_control import AccessControlService
from app.backend.services.media_manager import FileValidationConfig
from app.backend.services.media import formats, jobs, renderer, urls as thumb_urls
from app.backend.services.media.probe import has_no_video_stream, metadata_for, resolve_tools
from app.backend.enums.general import MediaType, Permissions, ThumbnailFormat, ThumbnailSize
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.auth.scope import Node
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.config import settings
from app.backend.api.shared.helpers.media_helper import storage_root
from app.backend.api.shared.helpers.post_helper import ensure_post_not_archived
from app.backend.enums.errors import ErrorCode
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import CodedError
from app.backend.utils.row_locks import lock_first, start_write

_logger = get_api_logger()


def validation_config_for(registry: formats.FormatRegistry, spec: formats.FormatSpec) -> FileValidationConfig:
    """Manager-side checks (size, extension, detected MIME) driven by the resolved format"""
    return FileValidationConfig(
        max_size=spec.effective_limit_bytes,
        sanitize_filename=True,
        validate_content_headers=True,
        strict_mime_validation=True,
        allowed_extensions=set(spec.extensions),
        allowed_mime_types=set(spec.all_mimes),
    )


def format_rejected(registry: formats.FormatRegistry, exc: formats.UnsupportedMediaFormat) -> ApiError:
    params = {k: v for k, v in exc.params.items() if k != "media_type"}
    return _unsupported(registry, exc.code, exc.media_type, **params)


def store_rejected(exc: CodedError, headers: dict = None) -> ApiError:
    """A file refused by MediaManager.store_file_from_path; the status comes with its problem type"""
    return ApiError.from_coded(exc, headers=headers)


def too_large(spec: formats.FormatSpec, actual: int = None) -> ApiError:
    return ApiError(
        ErrorCode.MEDIA_FILE_TOO_LARGE,
        content_type=spec.content_type,
        max_file_size_bytes=spec.effective_limit_bytes,
        file_size_bytes=actual,
    )


def check_declared_format(registry: formats.FormatRegistry, media_type: str, filename: str, content_type: str = None) -> formats.FormatSpec:
    """Reject before touching the body: unknown media_type, extension or declared Content-Type outside the registry"""
    media_type = (media_type or "").lower()
    if media_type not in registry.media_types():
        raise _unsupported(registry, ErrorCode.MEDIA_TYPE_UNKNOWN, media_type)
    spec = registry.resolve(media_type, filename)
    if spec is None:
        raise _unsupported(registry, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, media_type, extension=Path(filename or "").suffix.lower())
    declared = formats.normalise_content_type(content_type)
    if declared and declared not in formats.OPAQUE_CONTENT_TYPES and declared not in spec.all_mimes:
        if declared in registry.mime_set(media_type):
            raise _unsupported(registry, ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, media_type,
                               content_type=declared, expected_content_type=spec.content_type)
        raise _unsupported(registry, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED, media_type, content_type=declared)
    return spec


async def load_observation_for_upload(session: AsyncSession, auth: AuthContext, observation_id: int) -> Observation:
    """The observation an upload goes to: 404 when missing or archived, 403 without media.view and media.create in
    effect at its post, 409 post_archived when its post is archived"""
    observation = (await session.exec(
        select(Observation).where(Observation.id == observation_id, Observation.is_deleted == False)
    )).first()
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
    AccessControlService.check_post(auth, Permissions.MEDIA_CREATE, observation.post_id, "media")
    await ensure_post_not_archived(session, observation.post_id)
    return observation


async def record_upload(
    request: Request,
    session: AsyncSession,
    auth: AuthContext,
    media_file_id: int,
    observation_id: int,
    post_id: int,
    original_filename: str,
    background_tasks: BackgroundTasks,
    transport: str,
    upload_id: Optional[str] = None,
) -> MediaFile:
    """The last transaction of an upload, once the file is in storage: probed video/audio metadata, the release of
    the poster hold and the audit row, committed together; then the medium thumbnail is pre-warmed.

    The probe runs before that transaction and outside any other: the row is read and the read transaction ended
    first, so no read view lives across ffprobe (seconds for a video), and the write begins with the row lock.
    When this transaction fails (500), the file and its row stay stored (a move cannot be rolled back) without
    metadata and audit row, and the poster job stays held: the hold runs out after jobs.UPLOAD_HOLD and the worker
    takes the poster like any other (it probes the metadata too; `media_tools probe-missing` fills metadata of
    other media types)."""
    media_file = (await session.exec(select(MediaFile).where(MediaFile.id == media_file_id))).one()
    await start_write(session)
    metadata = None
    if media_file.media_type in (MediaType.VIDEO.value, MediaType.AUDIO.value):
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
                "via": transport, **({"upload_id": upload_id} if upload_id else {})},
        scope=Node.post(post_id), resource_id=media_file_id,
        user_id=auth.user.id, new_values=media_file.model_dump(),
        **client_info(request, auth),
    )
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
