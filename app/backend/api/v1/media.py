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
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.services.access_control import AccessControlService
from app.backend.services.media.manager import get_media_manager
from app.backend.services.media import formats, jobs, renderer, urls as thumb_urls
from app.backend.services.media.formats import UnsupportedMediaFormat, FileTooLarge
from app.backend.enums.general import Permissions, ThumbnailFormat, ThumbnailSize, ThumbnailStatus
from app.backend.enums.routes import MediaRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
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
    ORIGINAL_FILE_HEADERS,
    THUMBNAIL_PENDING_HEADERS,
)
from app.backend.api.v1.models.query.media import MediaFilterParams
from app.backend.filters.media_filter import MediaFilterService
from app.backend.api.shared.helpers.upload_helper import (
    check_declared_format, format_rejected, load_observation_for_upload, record_upload, store_rejected,
    too_large, validation_config_for,
)
from app.backend.api.shared.helpers.media_helper import (
    get_observation_from_media,
    get_media_or_404,
    original_file_response,
    storage_root,
)
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.services.api_errors import ApiError, CodedError
from app.backend.api.shared.openapi_docs import empty_response, file_response, header
from app.backend.openapi_problems import ADMIN, GUARDED, problems

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
    registry = await formats.load_registry(session)
    response.headers["Accept-Post"] = registry.accept_post_header()
    return MediaFormatsResponse(
        media_types=registry.media_types(),
        formats=registry.payload(),
        max_file_size_bytes=formats.hard_limit_bytes(),
    )


@router.get(MediaRoutes.ROOT.value, response_model=List[MediaFileInfo], responses=problems(*GUARDED))
async def list_media_files(
    filters: Annotated[MediaFilterParams, Query()],
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """List media files with filters. Each item carries thumbnail_status and signed thumbnail_urls."""
    query = select(MediaFile).where(MediaFile.is_deleted == False)
    query = MediaFilterService.apply_filters(query, filters, AccessControlService.post_scope(auth))
    result = await session.exec(query)
    return [MediaFileInfo.model_validate(mf) for mf in result.all()]


@router.get(MediaRoutes.STORAGE_STATISTICS.value, response_model=StorageStatisticsResponse, responses=problems(*ADMIN))
async def get_storage_statistics(
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW], admin=True))
):
    """Get storage statistics and health information"""
    return await get_media_manager().get_storage_statistics(session)


# ---------------------------------------------------------------------------
# Poster queue administration (declared before the /{media_id} routes)
# ---------------------------------------------------------------------------

@router.get(MediaRoutes.POSTERS_STATUS.value, response_model=PosterStatusResponse, responses=problems(*ADMIN))
async def get_poster_status(
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW], admin=True))
):
    """Poster queue depth by status and the media worker heartbeats (administration)."""
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


@router.post(MediaRoutes.POSTERS_RECONCILE.value, response_model=PosterReconcileResponse, responses=problems(*ADMIN))
async def reconcile_posters(
    request: Request,
    body: PosterReconcileRequest,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_UPDATE], admin=True))
):
    """Re-queue videos whose poster is missing on disk or untracked; optionally retry failed ones (administration)."""
    counts = await jobs.reconcile_posters(session, storage_root(), retry_failed=body.retry_failed)
    await create_audit_log(
        session=session, action="reconcile", resource_type="media_posters",
        user_id=auth.user.id, details={"message": f"User {auth.user.username} reconciled posters", **counts},
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
    AccessControlService.check_post_view(auth, observation.post_id, "media")
    return MediaFileInfo.model_validate(media_file)


@router.get(
    MediaRoutes.MEDIA_DOWNLOAD.value,
    response_class=Response,
    responses={
        **file_response("The original file (attachment), in its own Content-Type", ["*/*"], ORIGINAL_FILE_HEADERS),
        **file_response("The requested byte range (Range header)", ["*/*"], {"Content-Range": header("bytes start-end/size")}, status=206),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_FILE_MISSING,
            ErrorCode.MEDIA_NOT_FOUND, ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_PATH_UNRESOLVED, *GUARDED,
        ),
    },
)
async def download_media_file(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Download the original as an attachment. nginx serves it via X-Accel-Redirect; otherwise FileResponse with Range support."""
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media")
    return original_file_response(media_file, "attachment")


@router.get(
    MediaRoutes.MEDIA_STREAM.value,
    response_class=Response,
    responses={
        **file_response("The original file (inline), in its own Content-Type", ["*/*"], ORIGINAL_FILE_HEADERS),
        **file_response("The requested byte range (Range header)", ["*/*"], {"Content-Range": header("bytes start-end/size")}, status=206),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_FILE_MISSING,
            ErrorCode.MEDIA_NOT_FOUND, ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_PATH_UNRESOLVED, *GUARDED,
        ),
    },
)
async def stream_media_file(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW]))
):
    """Serve the original inline (video and audio players). Same transport as download, with Range support."""
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media")
    return original_file_response(media_file, "inline")


async def _set_media_archived(request: Request, session: AsyncSession, auth: AuthContext, media_id: int, archived: bool) -> MediaFileInfo:
    """Shared body of archive / restore: post write access, ownership (media.manage_any overrides), flag flip, audit row."""
    current_user = auth.user
    media_file = await get_media_or_404(session, media_id, include_deleted=True)
    if archived and media_file.is_deleted:
        raise ApiError(ErrorCode.MEDIA_NOT_FOUND)
    if not archived and not media_file.is_deleted:
        raise ApiError(ErrorCode.MEDIA_NOT_ARCHIVED)

    observation = None
    if media_file.observation_id:
        observation = await get_observation_from_media(session, media_file)
        AccessControlService.check_post_write(auth, observation.post_id, "media")
    AccessControlService.check_media_ownership(auth, media_file, observation)

    action = "archive" if archived else "restore"
    old_values = media_file.model_dump()
    media_file.is_deleted = archived
    session.add(media_file)
    await create_audit_log(
        session=session, action=action, resource_type="media", resource_id=media_id,
        user_id=current_user.id, old_values=old_values, new_values=media_file.model_dump(),
        details={"message": f"User {current_user.username} {action}d media file '{old_values.get('original_filename')}'"},
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
        ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_NOT_ARCHIVED, *GUARDED,
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
    """
    return await _set_media_archived(request, session, auth, media_id, archived=True)


@router.post(
    MediaRoutes.MEDIA_RESTORE.value,
    response_model=MediaFileInfo,
    responses=problems(
        AccessErrorCode.MEDIA_NOT_OWNER, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_ACTIVE,
        AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.MEDIA_NOT_FOUND,
        ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_NOT_ARCHIVED, *GUARDED,
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
        ErrorCode.MEDIA_CONTENT_INVALID, ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING,
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN,
        ErrorCode.MEDIA_STORE_FAILED, ErrorCode.MEDIA_UPLOADS_BUSY, *GUARDED,
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
    while streaming; it is never held in memory. Users can only upload for observations of their post.
    """
    await load_observation_for_upload(session, auth, observation_id)

    original_filename = file.filename or "uploaded_file"
    registry = await formats.load_registry(session)
    spec = check_declared_format(registry, media_type, original_filename, file.content_type)
    media_type = spec.media_type
    if file.size and file.size > spec.max_file_size_bytes:
        raise too_large(spec, file.size)

    media_manager = get_media_manager()
    temp_path = await media_manager.spool_upload(file)
    spooled = temp_path.stat().st_size
    if spooled > spec.max_file_size_bytes:
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
            validation_config=validation_config_for(registry, spec),
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

    media_file = await record_upload(request, session, auth, media_file_id, observation_id, original_filename, background_tasks, "multipart")
    return upload_result(media_file, "multipart")


@router.post(
    MediaRoutes.NGINX_UPLOAD.value,
    response_model=UploadResultResponse,
    responses=problems(
        ErrorCode.MEDIA_CONTENT_INVALID, ErrorCode.MEDIA_FILE_EMPTY, ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING,
        ErrorCode.NGINX_FILE_NAME_MISSING, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.MEDIA_FILE_TOO_LARGE,
        ErrorCode.MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH, ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED,
        ErrorCode.MEDIA_EXTENSION_DANGEROUS, ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, ErrorCode.MEDIA_TYPE_UNKNOWN,
        ErrorCode.MEDIA_STORE_FAILED, ErrorCode.NGINX_UPLOAD_DISABLED,
        ErrorCode.MEDIA_UPLOADS_BUSY, *GUARDED,
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
    file and forwards its path in X-File-Name; the original filename comes in X-Original-Filename.
    """
    if not settings.storage.enable_nginx:
        raise ApiError(ErrorCode.NGINX_UPLOAD_DISABLED)

    temp_file_path = x_file_name
    if not temp_file_path:
        raise ApiError(ErrorCode.NGINX_FILE_NAME_MISSING)
    original_filename = x_original_filename or "uploaded_file"
    registry = await formats.load_registry(session)
    spec = check_declared_format(registry, media_type, original_filename, request.headers.get("content-type"))
    media_type = spec.media_type

    await load_observation_for_upload(session, auth, observation_id)

    try:
        media_file_id = await get_media_manager().store_file_from_path(
            session=session,
            observation_id=observation_id,
            source_path=temp_file_path,
            original_filename=original_filename,
            media_type=media_type,
            user_id=auth.user.id,
            validation_config=validation_config_for(registry, spec),
        )
    except FileTooLarge as e:
        raise too_large(spec, e.actual_bytes)
    except UnsupportedMediaFormat as e:
        _logger.warning(f"Nginx upload rejected (format): {e}")
        raise format_rejected(registry, e)
    except CodedError as e:
        _logger.warning(f"Nginx upload rejected: {e}")
        raise store_rejected(e)

    media_file = await record_upload(request, session, auth, media_file_id, observation_id, original_filename, background_tasks, "nginx offload")
    return upload_result(media_file, "nginx offload")


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
    AccessControlService.check_post_view(auth, observation.post_id, "observation media")

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
    return Response(content=rendered.body, media_type=rendered.headers.get("content-type", "image/jpeg"), headers=headers)


@router.get(
    MediaRoutes.MEDIA_THUMBNAIL.value,
    response_class=Response,
    responses={
        **file_response("The image (WebP when format=auto and the client accepts it)", ["image/webp", "image/jpeg", "image/png"],
                        {"X-Thumbnail-Status": header("ready"), "Cache-Control": header("private, max-age=86400")}),
        **empty_response(202, "The video poster is not extracted yet; ask again after Retry-After", THUMBNAIL_PENDING_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND,
            ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.POSTER_FAILED, ErrorCode.THUMBNAILS_UNSUPPORTED,
            ErrorCode.THUMBNAIL_RENDERER_ERROR, ErrorCode.THUMBNAIL_RENDERER_UNAVAILABLE, *GUARDED,
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
    poster is not extracted yet. 409: poster extraction failed. 422: audio or documents.
    """
    media_file = await get_media_or_404(session, media_id)
    observation = await get_observation_from_media(session, media_file)
    AccessControlService.check_post_view(auth, observation.post_id, "media thumbnail")

    if media_file.thumbnail_status != ThumbnailStatus.READY.value:
        return _not_ready_response(media_file)

    if settings.storage.enable_nginx:
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


@router.post(
    MediaRoutes.MEDIA_SHARE.value,
    response_model=ShareLinkResponse,
    responses={
        **empty_response(202, "The video poster is not extracted yet; ask again after Retry-After", THUMBNAIL_PENDING_HEADERS),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND,
            ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.POSTER_FAILED, ErrorCode.THUMBNAILS_UNSUPPORTED, *GUARDED,
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
    AccessControlService.check_post_view(auth, observation.post_id, "media thumbnail")

    if media_file.thumbnail_status != ThumbnailStatus.READY.value:
        return _not_ready_response(media_file)

    ttl = min(body.ttl_seconds, settings.storage.thumbnail.share_max_ttl_seconds)
    url, expires_at = thumb_urls.public_url(media_file, body.size.value, body.format.value, ttl)

    await create_audit_log(
        session=session, action="share", resource_type="media", resource_id=media_id,
        user_id=auth.user.id,
        details={"message": f"User {auth.user.username} shared a {body.size.value} thumbnail of media #{media_id}", "ttl_seconds": ttl},
        **client_info(request, auth),
    )
    await session.commit()
    return ShareLinkResponse(media_id=media_id, size=body.size.value, format=body.format.value, url=url, expires_at=expires_at)


@router.post(
    MediaRoutes.MEDIA_THUMBNAIL_REGENERATE.value,
    response_model=ThumbnailRegenerateResponse,
    responses=problems(ErrorCode.MEDIA_NOT_FOUND, ErrorCode.THUMBNAILS_UNSUPPORTED, *ADMIN),
)
async def regenerate_thumbnail(
    media_id: int,
    session: AsyncSession = Depends(get_session),
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW, Permissions.MEDIA_UPDATE], admin=True)),
):
    """Video: delete the poster and queue extraction again. Images are rendered on demand, nothing to regenerate."""
    media_file = await get_media_or_404(session, media_id, include_deleted=True)

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
