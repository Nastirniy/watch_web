"""Media routes.

Originals are served by nginx (X-Accel-Redirect) or FileResponse. Thumbnails
are never generated here: imgproxy renders them on demand from the original
(images) or from the poster the media worker extracted (videos), and nginx
caches the result. This module only authorises, signs URLs and redirects.
"""
from typing import List, Optional, Annotated

import httpx
from fastapi import APIRouter, Depends, status as fapi_status, UploadFile, File, Form, BackgroundTasks, Header, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.storyboard import VideoStoryboard
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.audit import AuditEvent
from app.backend.services.units import observation_units_filter
from app.backend.services.media.manager import get_media_manager
from app.backend.services.files import formats
from app.backend.services.media import jobs, renderer, uploads, urls as thumb_urls
from app.backend.services.media.storyboards import build_vtt, layout_usable, status_for
from app.backend.services.files.formats import UnsupportedMediaFormat, FileTooLarge
from app.backend.services.files.names import header_filename
from app.backend.enums.general import Permissions, UploadTransport, PosterStatus, StoryboardStatus, ThumbnailFormat, ThumbnailSize, ThumbnailStatus
from app.backend.enums.media_types import ContentType, Disposition
from app.backend.enums.routes import MediaRoutes
from app.backend.utils.content_type import served_content_type
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.config import settings
from app.backend.api.v1.models.request.media import ListAttachedMediaRequest, ShareLinkRequest, PosterReconcileRequest
from app.backend.api.v1.models.response.media import (
    MediaFileInfo,
    ShareLinkResponse,
    PosterStatusResponse,
    PosterReconcileResponse,
    ThumbnailRegenerateResponse,
    WorkerInfo,
    MediaFormatsResponse,
    StorageStatisticsResponse,
    UploadResultResponse,
    upload_result,
    FILE_NOT_MODIFIED,
    FILE_NOT_MODIFIED_HEADERS,
    ORIGINAL_FILE_HEADERS,
    PARTIAL_FILE_HEADERS,
    THUMBNAIL_PENDING_HEADERS,
    STORYBOARD_HEADERS,
    STORYBOARD_PENDING_HEADERS,
)
from app.backend.api.v1.models.query.media import MediaFilterParams
from app.backend.filters.media_filter import MediaFilterService
from app.backend.api.shared.helpers.upload_helper import (
    check_declared_format, format_rejected, load_observation_for_upload, record_upload, store_rejected,
    too_large, validation_config_for,
)
from app.backend.api.shared.helpers.post_helper import ensure_post_not_archived
from app.backend.api.shared.helpers.media_helper import (
    get_observation_from_media,
    get_media_or_404,
    if_none_match_of,
    original_file_response,
    source_file_exists,
    storage_root,
)
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import CodedError
from app.backend.api.shared.openapi_docs import empty_response, file_response, header
from app.backend.errors.openapi import GUARDED, problems

router = APIRouter(prefix=MediaRoutes.BASE.value, tags=[MediaRoutes.tag()])
_logger = get_api_logger()


# ---------------------------------------------------------------------------
# Listing and metadata
# ---------------------------------------------------------------------------


@router.get(MediaRoutes.FORMATS.value, response_model=MediaFormatsResponse, responses=problems(*GUARDED))
async def get_media_formats(
    response: Response,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Accepted upload formats (active rows of media_formats): Content-Type, extensions and size limit per media type. Also sent as Accept-Post."""
    registry = await formats.observation_registry(session)
    response.headers["Accept-Post"] = registry.accept_post_header()
    return MediaFormatsResponse(
        media_types=registry.media_types(),
        formats=registry.payload(),
        max_file_size_bytes=formats.hard_limit_bytes(),
    )


@router.get(
    MediaRoutes.ROOT.value, response_model=List[MediaFileInfo],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_media_files(
    filters: Annotated[MediaFilterParams, Query()],
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """List media files with filters. Each item carries thumbnail_status and signed thumbnail_urls.

    Media of the posts where media.view is in effect; archived media only with include_deleted (requires
    media.archive), and only at the posts where media.archive is in effect."""
    archived = AccessControlService.archived_scope(auth, filters, Permissions.MEDIA_ARCHIVE)
    query = select(MediaFile)
    query = MediaFilterService.apply_filters(
        query, filters, AccessControlService.scope_for(auth, Permissions.MEDIA_VIEW), archived_post_ids=archived,
        unit_condition=observation_units_filter(auth.tree, filters),
    )
    result = await session.exec(query)
    return [MediaFileInfo.model_validate(mf) for mf in result.all()]


@router.get(MediaRoutes.STORAGE_STATISTICS.value, response_model=StorageStatisticsResponse, responses=problems(*GUARDED))
async def get_storage_statistics(
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_MONITOR]))
):
    """Get storage statistics and health information (media.monitor)"""
    return await get_media_manager().get_storage_statistics(session)


# ---------------------------------------------------------------------------
# Poster queue administration (declared before the /{media_id} routes)
# ---------------------------------------------------------------------------

@router.get(MediaRoutes.POSTERS_STATUS.value, response_model=PosterStatusResponse, responses=problems(*GUARDED))
async def get_poster_status(
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_MONITOR]))
):
    """Poster queue depth by status and the media worker heartbeats (media.monitor)."""
    counts = await jobs.poster_counts(session)
    workers = []
    for row in await jobs.worker_heartbeats(session):
        workers.append(WorkerInfo(
            name=row.name, kind=row.kind, hostname=row.hostname, pid=row.pid,
            started_at=row.started_at, last_seen_at=row.last_seen_at,
            jobs_done=row.jobs_done, jobs_failed=row.jobs_failed,
            stale=jobs.is_stale(row.last_seen_at), details=row.details,
        ))
    alive = any(not w.stale and w.kind == "media" for w in workers)
    return PosterStatusResponse(posters=counts, workers=workers, worker_alive=alive)


@router.post(MediaRoutes.POSTERS_RECONCILE.value, response_model=PosterReconcileResponse, responses=problems(*GUARDED))
async def reconcile_posters(
    request: Request,
    body: PosterReconcileRequest,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(at_root=[Permissions.MEDIA_VIEW, Permissions.MEDIA_UPDATE, Permissions.MEDIA_MONITOR]))
):
    """Re-queue videos whose poster is missing on disk or untracked; optionally retry failed ones. Acts on every post,
    so media.view, media.update and media.monitor must all be in effect at the root (403 permission_denied, the
    `at_root` of required): the same permissions held at a post or a group do not count."""
    counts = await jobs.reconcile_posters(session, storage_root(), retry_failed=body.retry_failed)
    await create_audit_log(
        session=session, event=AuditEvent.MEDIA_POSTERS_RECONCILE, params={k: int(v) for k, v in counts.items()},
        user_id=auth.user.id,
        **client_info(request, auth),
    )
    await session.commit()
    return PosterReconcileResponse(**counts)


# ---------------------------------------------------------------------------
# Single media
# ---------------------------------------------------------------------------

@router.get(
    MediaRoutes.MEDIA_INFO.value,
    response_model=MediaFileInfo,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND,
        ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, *GUARDED,
    ),
)
async def get_media_info(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Information about a media file, including thumbnail readiness and signed thumbnail URLs."""
    media_query = select(MediaFile).options(selectinload(MediaFile.user)).where(
        MediaFile.id == media_id,
        MediaFile.is_deleted == False
    )
    media_file = (await session.exec(media_query)).first()
    if not media_file:
        raise ApiError(ErrorCode.MEDIA_NOT_FOUND)

    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
    return MediaFileInfo.model_validate(media_file)


@router.get(
    MediaRoutes.MEDIA_DOWNLOAD.value,
    response_class=Response,
    responses={
        **file_response("The original file (attachment), in its own Content-Type", [ContentType.ANY], ORIGINAL_FILE_HEADERS),
        **file_response("The requested byte range (Range header)", [ContentType.ANY], PARTIAL_FILE_HEADERS, status=206),
        **empty_response(304, FILE_NOT_MODIFIED, FILE_NOT_MODIFIED_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_FILE_MISSING,
            ErrorCode.MEDIA_NOT_FOUND, ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_PATH_UNRESOLVED,
            ErrorCode.RANGE_NOT_SATISFIABLE, *GUARDED,
        ),
    },
)
async def download_media_file(
    request: Request,
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Download the original as an attachment (nginx serves it via X-Accel-Redirect; otherwise the API, with Range
    support). ETag and Repr-Digest name the file, on a 206 as well. If-None-Match with the ETag (or *) answers 304
    without the file, after the access checks; If-Range with the ETag works as usual."""
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
    return original_file_response(media_file, Disposition.ATTACHMENT, request.headers.get("range"),
                                  if_none_match_of(request))


@router.get(
    MediaRoutes.MEDIA_STREAM.value,
    response_class=Response,
    responses={
        **file_response("The original file (inline), in its own Content-Type", [ContentType.ANY], ORIGINAL_FILE_HEADERS),
        **file_response("The requested byte range (Range header)", [ContentType.ANY], PARTIAL_FILE_HEADERS, status=206),
        **empty_response(304, FILE_NOT_MODIFIED, FILE_NOT_MODIFIED_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_FILE_MISSING,
            ErrorCode.MEDIA_NOT_FOUND, ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_PATH_UNRESOLVED,
            ErrorCode.RANGE_NOT_SATISFIABLE, *GUARDED,
        ),
    },
)
async def stream_media_file(
    request: Request,
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Serve the original inline (video and audio players). Same transport as download, with Range support. ETag and
    Repr-Digest name the file, on a 206 as well. If-None-Match with the ETag (or *) answers 304 without the file,
    after the access checks: nginx sends a request with If-None-Match to the API (never its 30 second cache of the
    access answer); If-Range with the ETag works as usual."""
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
    return original_file_response(media_file, Disposition.INLINE, request.headers.get("range"),
                                  if_none_match_of(request))


async def _set_media_archived(request: Request, session: AsyncSession, auth: AuthContext, media_id: int, archived: bool) -> MediaFileInfo:
    """Shared body of archive / restore: media.view and media.archive in effect at the post, ownership
    (media.manage_any in effect there overrides), flag flip, audit row. Media of an archived post: 409 post_archived
    (after the 403 checks)."""
    current_user = auth.user
    media_file = await get_media_or_404(session, media_id, include_deleted=True, lock=True)

    observation = None
    if media_file.observation_id:
        observation = await get_observation_from_media(session, media_file)
        AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
        AccessControlService.check_post(auth, Permissions.MEDIA_ARCHIVE, observation.post_id, "media")
    AccessControlService.check_media_ownership(auth, media_file, observation)
    if observation is not None:
        await ensure_post_not_archived(session, observation.post_id)

    # The state last: an outsider gets the same 403 whatever the row looks like
    if archived and media_file.is_deleted:
        raise ApiError(ErrorCode.MEDIA_NOT_FOUND)
    if not archived and not media_file.is_deleted:
        raise ApiError(ErrorCode.MEDIA_NOT_ARCHIVED)

    old_values = media_file.model_dump()
    media_file.is_deleted = archived
    session.add(media_file)
    await create_audit_log(
        session, AuditEvent.MEDIA_ARCHIVE if archived else AuditEvent.MEDIA_RESTORE,
        params={"observation_id": media_file.observation_id} if media_file.observation_id is not None else {},
        scope=Node.post(observation.post_id) if observation is not None else ROOT, resource_id=media_id,
        user_id=current_user.id, old_values=old_values, new_values=media_file.model_dump(),
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(media_file)
    return MediaFileInfo.model_validate(media_file)


@router.post(
    MediaRoutes.MEDIA_ARCHIVE.value,
    response_model=MediaFileInfo,
    responses=problems(
        AccessErrorCode.MEDIA_NOT_OWNER, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_ACTIVE,
        AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.MEDIA_NOT_FOUND,
        ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_NOT_ARCHIVED, ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def archive_media_file(
    request: Request,
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_ARCHIVE]))
):
    """Archive a media file (is_deleted=true): hidden from its observation and from lists, the file stays on disk
    and stays linked to the observation so it can be restored. Regular users may only archive files they uploaded
    within their post while the observation is active; media.manage_any overrides. Undo with POST /{media_id}/restore.
    Media of an archived post: 409 post_archived.
    """
    return await _set_media_archived(request, session, auth, media_id, archived=True)


@router.post(
    MediaRoutes.MEDIA_RESTORE.value,
    response_model=MediaFileInfo,
    responses=problems(
        AccessErrorCode.MEDIA_NOT_OWNER, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_ACTIVE,
        AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.MEDIA_NOT_FOUND,
        ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_NOT_ARCHIVED, ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def restore_media_file(
    request: Request,
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_ARCHIVE]))
):
    """Restore an archived media file (is_deleted=false). Same ownership rules as archive. Not archived: 409."""
    return await _set_media_archived(request, session, auth, media_id, archived=False)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post(
    MediaRoutes.UPLOAD.value,
    response_model=UploadResultResponse,
    responses=problems(
        ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING,
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN,
        ErrorCode.MEDIA_STORE_FAILED, ErrorCode.MEDIA_UPLOADS_BUSY, ErrorCode.MOVE_PENDING, *GUARDED,
    ),
)
async def upload_complete_file(
    request: Request,
    background_tasks: BackgroundTasks,
    observation_id: int = Form(...),
    media_type: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_CREATE])),
):
    """
    Multipart upload. The body is spooled to a temp file in 1 MB chunks and hashed
    while streaming; it is never held in memory. Users can only upload for observations of their post; an archived
    post: 409 post_archived; a move waiting above the post: 409 move_pending with Retry-After.
    """
    post_id = (await load_observation_for_upload(session, auth, observation_id)).post_id

    original_filename = file.filename or "uploaded_file"
    registry = await formats.observation_registry(session)
    spec = check_declared_format(registry, media_type, original_filename, file.content_type)
    media_type = spec.media_type
    if file.size and file.size > spec.effective_limit_bytes:
        raise too_large(spec, file.size)

    media_manager = get_media_manager()
    temp_path = await media_manager.spool_upload(file)
    spooled = temp_path.stat().st_size
    if spooled > spec.effective_limit_bytes:
        temp_path.unlink(missing_ok=True)
        raise too_large(spec, spooled)
    _logger.info(f"Uploading file: '{original_filename}' ({spooled} bytes)")

    try:
        media_file_id = await media_manager.store_file_from_path(
            session=session,
            observation_id=observation_id,
            source_path=str(temp_path),
            original_filename=original_filename,
            media_type=media_type,
            user_id=auth.user.id,
            validation_config=validation_config_for(spec),
            refuse_waiting_move=True,
        )
    except FileTooLarge as e:
        raise too_large(spec, e.actual_bytes)
    except UnsupportedMediaFormat as e:
        _logger.warning(f"Upload rejected (format): {e}")
        raise format_rejected(registry, e)
    except CodedError as e:
        _logger.warning(f"Upload rejected: {e}")
        raise store_rejected(e)
    finally:
        temp_path.unlink(missing_ok=True)

    media_file = await record_upload(request, session, auth, media_file_id, observation_id, post_id, original_filename, background_tasks, UploadTransport.MULTIPART)
    return upload_result(media_file, UploadTransport.MULTIPART)


@router.post(
    MediaRoutes.NGINX_UPLOAD.value,
    response_model=UploadResultResponse,
    responses=problems(
        ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING,
        ErrorCode.NGINX_FILE_NAME_MISSING, ErrorCode.NGINX_FILE_NAME_INVALID, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN,
        ErrorCode.MEDIA_STORE_FAILED, ErrorCode.NGINX_UPLOAD_DISABLED,
        ErrorCode.MEDIA_UPLOADS_BUSY, ErrorCode.MOVE_PENDING, *GUARDED,
    ),
)
async def nginx_upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    observation_id: int = Query(..., description="Observation ID to attach file to"),
    media_type: str = Query(..., description="Media type: image, video or audio (see GET /media/formats)"),
    x_file_name: Optional[str] = Header(None, include_in_schema=False),  # set by nginx, never by the client
    x_original_filename: Optional[str] = Header(None, include_in_schema=False),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_CREATE])),
):
    """
    Upload via nginx offloading (client_body_in_file_only): nginx writes the raw body to a temp
    file and forwards its path in X-File-Name; the original filename comes in X-Original-Filename (its UTF-8 bytes,
    or ASCII). A move waiting
    above the post: 409 move_pending with Retry-After.
    """
    if not settings.storage.enable_nginx:
        raise ApiError(ErrorCode.NGINX_UPLOAD_DISABLED)

    if not x_file_name:
        raise ApiError(ErrorCode.NGINX_FILE_NAME_MISSING)
    # Only a file nginx wrote in its upload temp directory is ever touched; the header is not trusted as a path
    temp_file = uploads.resolve_nginx_temp_file(x_file_name, settings.storage.nginx_upload_temp_path)
    if temp_file is None:
        _logger.warning(f"Nginx upload refused: X-File-Name {x_file_name!r} is not a file in the upload temp directory")
        raise ApiError(ErrorCode.NGINX_FILE_NAME_INVALID)

    try:
        original_filename = header_filename(x_original_filename) or "uploaded_file"
        registry = await formats.observation_registry(session)
        spec = check_declared_format(registry, media_type, original_filename, request.headers.get("content-type"))
        media_type = spec.media_type

        post_id = (await load_observation_for_upload(session, auth, observation_id)).post_id

        try:
            media_file_id = await get_media_manager().store_file_from_path(
                session=session,
                observation_id=observation_id,
                source_path=str(temp_file),
                original_filename=original_filename,
                media_type=media_type,
                user_id=auth.user.id,
                validation_config=validation_config_for(spec),
                refuse_waiting_move=True,
            )
        except FileTooLarge as e:
            raise too_large(spec, e.actual_bytes)
        except UnsupportedMediaFormat as e:
            _logger.warning(f"Nginx upload rejected (format): {e}")
            raise format_rejected(registry, e)
        except CodedError as e:
            _logger.warning(f"Nginx upload rejected: {e}")
            raise store_rejected(e)
    finally:
        # A successful store moved the body file; every other outcome deletes it here at once (nginx deletes it too
        # when the request ends, client_body_in_file_only clean, and a missing file is fine for both). A failed
        # unlink never replaces the outcome: it is logged and nginx or sweep-nginx-temp removes the file later.
        try:
            temp_file.unlink(missing_ok=True)
        except OSError as exc:
            _logger.warning(f"Nginx upload: could not remove temp file {temp_file}: {exc!r}")

    media_file = await record_upload(request, session, auth, media_file_id, observation_id, post_id, original_filename,
                                     background_tasks, UploadTransport.NGINX_OFFLOAD)
    return upload_result(media_file, UploadTransport.NGINX_OFFLOAD)


@router.post(
    MediaRoutes.LIST_ALL_ATTACHED_MEDIA.value,
    response_model=List[MediaFileInfo],
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, *GUARDED,
    ),
)
async def list_media_for_observation(
    request: ListAttachedMediaRequest,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """List all media files for a specific observation. Users can only list media from their post."""
    observation = (await session.exec(
        select(Observation).where(Observation.id == request.observation_id, Observation.is_deleted == False)
    )).first()
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "observation media")

    media_query = select(MediaFile).options(selectinload(MediaFile.user)).where(
        MediaFile.observation_id == request.observation_id,
        MediaFile.is_deleted == False
    )
    return [MediaFileInfo.model_validate(m) for m in (await session.exec(media_query)).all()]


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

def _not_ready_response(media_file: MediaFile) -> Response:
    status_value = media_file.thumbnail_status
    if status_value == ThumbnailStatus.UNSUPPORTED.value:
        if media_file.too_large_to_render:
            raise ApiError(
                ErrorCode.IMAGE_TOO_LARGE_FOR_THUMBNAILS,
                megapixels=media_file.megapixels,
                max_megapixels=float(settings.storage.imgproxy.max_src_resolution_mp),
            )
        if media_file.is_video:
            raise ApiError(ErrorCode.NO_VIDEO_STREAM)
        raise ApiError(ErrorCode.THUMBNAILS_UNSUPPORTED)
    if status_value == ThumbnailStatus.FAILED.value:
        raise ApiError(
            ErrorCode.POSTER_FAILED,
            headers={"X-Thumbnail-Status": status_value},
            thumbnail_status=status_value,
            attempts=media_file.poster_attempts,
        )
    # pending or processing: the media worker has not produced the poster yet. No side effects.
    return Response(
        status_code=fapi_status.HTTP_202_ACCEPTED,
        headers={"Retry-After": "2", "X-Thumbnail-Status": status_value, "Cache-Control": "no-store"},
    )


async def _proxy_render(url: str) -> Response:
    """Without nginx the API fetches the rendered image from imgproxy and returns it as is"""
    try:
        rendered = await renderer.fetch(url)
    except httpx.HTTPError as exc:
        _logger.error(f"thumbnail renderer unreachable: {exc}")
        raise ApiError(ErrorCode.THUMBNAIL_RENDERER_UNAVAILABLE)
    if rendered.status_code != 200:
        _logger.error(f"thumbnail renderer returned {rendered.status_code} for {url}")
        raise ApiError(ErrorCode.THUMBNAIL_RENDERER_ERROR)
    headers = {"Cache-Control": "private, max-age=86400", "X-Thumbnail-Status": ThumbnailStatus.READY.value}
    for name in ("etag", "content-length"):
        if name in rendered.headers:
            headers[name] = rendered.headers[name]
    return Response(content=rendered.body, media_type=rendered.headers.get("content-type", ContentType.IMAGE_JPEG.value), headers=headers)


@router.get(
    MediaRoutes.MEDIA_THUMBNAIL.value,
    response_class=Response,
    responses={
        **file_response("The image (WebP when format=auto and the client accepts it)", [ContentType.IMAGE_WEBP, ContentType.IMAGE_JPEG, ContentType.IMAGE_PNG],
                        {"X-Thumbnail-Status": header("ready"), "Cache-Control": header("private, max-age=86400")}),
        **empty_response(202, "The video poster is not extracted yet; ask again after Retry-After", THUMBNAIL_PENDING_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND,
            ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.POSTER_FAILED, ErrorCode.THUMBNAILS_UNSUPPORTED,
            ErrorCode.IMAGE_TOO_LARGE_FOR_THUMBNAILS, ErrorCode.NO_VIDEO_STREAM,
            ErrorCode.THUMBNAIL_RENDERER_ERROR, ErrorCode.THUMBNAIL_RENDERER_UNAVAILABLE, ErrorCode.MEDIA_FILE_MISSING,
            *GUARDED,
        ),
    },
)
async def get_media_thumbnail(
    media_id: int,
    size: ThumbnailSize = Query(ThumbnailSize.MEDIUM, description="Bounding box: small, medium, large or preview"),
    format: ThumbnailFormat = Query(ThumbnailFormat.AUTO, description="auto picks WebP when the client accepts it"),
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW])),
):
    """
    Thumbnail or preview of a media file, rendered on demand.

    200: image (nginx serves it from cache or imgproxy). 202 + Retry-After: the video
    poster is not extracted yet. 409: poster extraction failed. 422: audio, an image above the
    renderer's resolution limit, or a video file without a video stream.
    """
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media thumbnail")

    if media_file.thumbnail_status != ThumbnailStatus.READY.value:
        return _not_ready_response(media_file)

    if settings.storage.enable_nginx:
        # nginx would pass a missing source to imgproxy and cache its 404; the API answers the problem instead
        if not source_file_exists(media_file):
            raise ApiError(ErrorCode.MEDIA_FILE_MISSING)
        internal = thumb_urls.internal_url(media_file, size.value, format.value)
        return Response(
            status_code=200,
            headers={
                "X-Accel-Redirect": internal,
                "Cache-Control": "private, max-age=86400",
                "X-Thumbnail-Status": ThumbnailStatus.READY.value,
            },
        )
    return await _proxy_render(thumb_urls.renderer_url(media_file, size.value, format.value))


@router.get(
    MediaRoutes.MEDIA_STORYBOARD.value,
    response_class=Response,
    responses={
        **file_response("WebVTT storyboard: one cue per frame, signed sheet URLs with #xywh", [ContentType.WEBVTT], STORYBOARD_HEADERS),
        **empty_response(202, "The storyboard is not generated yet; ask again after Retry-After", STORYBOARD_PENDING_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND,
            ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.STORYBOARD_FAILED, ErrorCode.STORYBOARD_UNSUPPORTED,
            ErrorCode.NO_VIDEO_STREAM, *GUARDED,
        ),
    },
)
async def get_media_storyboard(
    request: Request,
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW])),
):
    """
    WebVTT of a video's seek previews: one cue per frame, each pointing at a tile of a signed sheet URL (#xywh).

    200: the WebVTT file. 202 + Retry-After: the worker has not produced the sheets yet. 409: generation failed.
    422: not a video, shorter than the threshold, or storyboards are turned off. No side effects.
    """
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media storyboard")

    config = settings.storage.storyboard
    # The row loaded here decides, not the job status read with the media row: it may have changed in between
    layout = await session.get(VideoStoryboard, media_id)
    status_value = status_for(media_file, config, layout.status if layout else None)
    if status_value == StoryboardStatus.UNSUPPORTED.value:
        if media_file.is_video and media_file.poster_status == PosterStatus.UNSUPPORTED.value:
            raise ApiError(ErrorCode.NO_VIDEO_STREAM)
        raise ApiError(ErrorCode.STORYBOARD_UNSUPPORTED, min_duration_seconds=config.min_duration_seconds)
    if status_value == StoryboardStatus.FAILED.value:
        raise ApiError(
            ErrorCode.STORYBOARD_FAILED,
            headers={"X-Storyboard-Status": status_value},
            attempts=layout.attempts if layout else 0,
        )
    if status_value == StoryboardStatus.READY.value and not layout_usable(layout, media_file.duration_seconds):
        # A ready row the WebVTT cannot be built from (a layout field or the duration missing): failed, never a 500
        _logger.error(f"storyboard of media {media_id} is ready without a usable layout or duration")
        raise ApiError(
            ErrorCode.STORYBOARD_FAILED,
            headers={"X-Storyboard-Status": StoryboardStatus.FAILED.value},
            attempts=layout.attempts,
        )
    if status_value != StoryboardStatus.READY.value:
        return Response(
            status_code=fapi_status.HTTP_202_ACCEPTED,
            headers={
                "Retry-After": str(config.retry_after_seconds),
                "X-Storyboard-Status": status_value,
                "Cache-Control": "no-store",
            },
        )

    ttl = settings.storage.thumbnail.url_ttl_seconds
    expires_at = thumb_urls.quantised_expiry(ttl)
    # Absolute cue URLs: the client hands the track to the player as a blob: URL, which cannot resolve relative ones.
    # The origin is public_base_url, else the request's (behind nginx: the Host and scheme nginx passed)
    origin = settings.storage.imgproxy.public_base_url or f"{request.url.scheme}://{request.url.netloc}"
    body = build_vtt(
        layout, media_file.duration_seconds,
        lambda index: thumb_urls.storyboard_sheet_url(
            media_id, index, expires_at, base_url=origin, generated_at=layout.generated_at,
        ),
    )
    return Response(
        content=body,
        # in the headers: Starlette would add a charset to media_type=, and WebVTT defines none (always UTF-8)
        headers={"Content-Type": served_content_type(ContentType.WEBVTT), "Cache-Control": f"private, max-age={ttl}"},
    )


@router.post(
    MediaRoutes.MEDIA_SHARE.value,
    response_model=ShareLinkResponse,
    responses={
        **empty_response(202, "The video poster is not extracted yet; ask again after Retry-After", THUMBNAIL_PENDING_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND,
            ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.POSTER_FAILED, ErrorCode.THUMBNAILS_UNSUPPORTED,
            ErrorCode.IMAGE_TOO_LARGE_FOR_THUMBNAILS, ErrorCode.NO_VIDEO_STREAM, *GUARDED,
        ),
    },
)
async def share_media_thumbnail(
    request: Request,
    media_id: int,
    body: ShareLinkRequest,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW])),
):
    """Signed, expiring public URL for a thumbnail or preview. Loads in an <img> without a bearer token."""
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media thumbnail")

    if media_file.thumbnail_status != ThumbnailStatus.READY.value:
        return _not_ready_response(media_file)

    ttl = min(body.ttl_seconds, settings.storage.thumbnail.share_max_ttl_seconds)
    # Never later than now + ttl (quantised_expiry may be up to twice that), still stable inside a window
    url, expires_at = thumb_urls.public_url(
        media_file, body.size.value, body.format.value, ttl, expires_at=thumb_urls.share_expiry(ttl),
    )

    await create_audit_log(
        session, AuditEvent.MEDIA_SHARE,
        params={"observation_id": media_file.observation_id, "ttl_seconds": ttl},
        scope=Node.post(observation.post_id), resource_id=media_id,
        user_id=auth.user.id,
        new_values={"size": body.size.value, "format": body.format.value, "expires_at": expires_at},
        **client_info(request, auth),
    )
    await session.commit()
    return ShareLinkResponse(media_id=media_id, size=body.size.value, format=body.format.value, url=url, expires_at=expires_at)


@router.post(
    MediaRoutes.MEDIA_THUMBNAIL_REGENERATE.value,
    response_model=ThumbnailRegenerateResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.MEDIA_NOT_FOUND,
        ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.THUMBNAILS_UNSUPPORTED, *GUARDED,
    ),
)
async def regenerate_thumbnail(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_UPDATE])),
):
    """Video: delete the poster and queue extraction again. Images are rendered on demand, nothing to regenerate.
    Needs media.view and media.update in effect at the post of the media (media.update at the root for media
    without an observation). Media of an archived post: 409 post_archived."""
    media_file = await get_media_or_404(session, media_id, include_deleted=True, lock=True)
    if media_file.observation_id:
        observation = await get_observation_from_media(session, media_file)
        AccessControlService.check_post(auth, Permissions.MEDIA_VIEW, observation.post_id, "media")
        AccessControlService.check_post(auth, Permissions.MEDIA_UPDATE, observation.post_id, "media")
        await ensure_post_not_archived(session, observation.post_id)
    else:
        AccessControlService.check_covers(auth, Permissions.MEDIA_UPDATE, ROOT, "regenerate_thumbnail")

    if media_file.is_video:
        poster = media_file.resolve_poster_path(storage_root())
        if poster and poster.exists():
            try:
                poster.unlink()
            except OSError as exc:
                _logger.warning(f"Failed to delete old poster {poster}: {exc}")
        await jobs.requeue_poster(session, media_file)
        await session.commit()
        return ThumbnailRegenerateResponse(media_id=media_id, thumbnail_status=media_file.thumbnail_status, message="Poster extraction queued")

    if media_file.is_image:
        return ThumbnailRegenerateResponse(
            media_id=media_id, thumbnail_status=media_file.thumbnail_status,
            message="Image thumbnails are rendered on demand; nothing to regenerate",
        )

    raise ApiError(ErrorCode.THUMBNAILS_UNSUPPORTED)
