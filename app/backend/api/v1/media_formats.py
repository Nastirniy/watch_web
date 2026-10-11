"""Administration of the accepted upload formats (media_formats).

Same shape as the other dictionaries (categories, tags): reads need
media_format.view, writes are administration routes and need view plus
create / update / archive. Nothing is ever removed: POST /{format_id}/archive
hides a row (is_deleted=true), POST /{format_id}/restore or creating the same
content_type again brings it back.

Every write invalidates the in-process registry cache; other API processes
pick the change up within REGISTRY_CACHE_TTL_SECONDS.
"""
from typing import List, Optional, Annotated

from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.db_errors import is_unique_violation
from app.backend.database import get_session
from app.backend.auth.access_control import AccessControlService
from app.backend.models.media_format import MediaFormat
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.services.media.formats import hard_limit_bytes, invalidate_registry_cache
from app.backend.enums.general import Permissions
from app.backend.enums.routes import MediaFormatRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.utils.datetime_utils import get_default_timezone_datetime
from app.backend.api.v1.models.request.media_formats import MediaFormatCreate, MediaFormatUpdate
from app.backend.api.v1.models.response.media_formats import MediaFormatResponse
from app.backend.api.v1.models.query.media_formats import MediaFormatFilterParams
from app.backend.filters.media_format_filter import MediaFormatFilterService
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import ADMIN, GUARDED, problems

router = APIRouter(prefix=MediaFormatRoutes.BASE.value, tags=[MediaFormatRoutes.tag()])


async def _check_conflicts(session: AsyncSession, media_type: str, extensions: List[str], aliases: List[str],
                           content_type: str, exclude_id: Optional[int] = None) -> None:
    """Extensions and MIME spellings must be unambiguous across the active, non-deleted formats"""
    rows = (await session.exec(select(MediaFormat).where(MediaFormat.is_deleted == False))).all()
    for row in rows:
        if row.id == exclude_id:
            continue
        clash_ext = sorted(set(extensions) & set(row.extensions or []))
        if clash_ext:
            raise ApiError(ErrorCode.MEDIA_FORMAT_EXTENSION_CLASH,
                           extensions=clash_ext, format_content_type=row.content_type, format_id=row.id)
        mine = {content_type, *aliases}
        theirs = {row.content_type, *(row.aliases or [])}
        clash_mime = sorted(mine & theirs)
        if clash_mime:
            raise ApiError(ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_CLASH,
                           content_types=clash_mime, format_content_type=row.content_type, format_id=row.id)


async def _commit_content_type(session: AsyncSession, content_type: str) -> None:
    """Commit; a unique violation on content_type (two requests passed the existence check together) answers
    media_format_exists with the row the other request made"""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        other = (await session.exec(select(MediaFormat).where(MediaFormat.content_type == content_type))).first()
        if other is None:
            raise
        raise ApiError(ErrorCode.MEDIA_FORMAT_EXISTS, content_type=content_type, format_id=other.id) from exc


def _check_size(max_file_size_bytes: int) -> None:
    ceiling = hard_limit_bytes()
    if max_file_size_bytes > ceiling:
        raise ApiError(ErrorCode.MEDIA_FORMAT_SIZE_ABOVE_CEILING,
                       max_file_size_bytes=max_file_size_bytes, ceiling_bytes=ceiling)


async def _get_format(session: AsyncSession, format_id: int, include_deleted: bool = False) -> MediaFormat:
    query = select(MediaFormat).where(MediaFormat.id == format_id)
    if not include_deleted:
        query = query.where(MediaFormat.is_deleted == False)
    row = (await session.exec(query)).first()
    if not row:
        raise ApiError(ErrorCode.MEDIA_FORMAT_NOT_FOUND)
    return row


@router.get(
    MediaFormatRoutes.LIST_ALL.value,
    response_model=List[MediaFormatResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_media_formats(
        filters: Annotated[MediaFormatFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW]))
):
    """Accepted formats with filters; include_deleted (archived rows too) requires media_format.archive."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.MEDIA_FORMAT_ARCHIVE)
    query = MediaFormatFilterService.apply_filters(select(MediaFormat), filters)
    rows = (await session.exec(query)).all()
    return [MediaFormatResponse.model_validate(row) for row in rows]


@router.post(
    MediaFormatRoutes.CREATE.value,
    response_model=MediaFormatResponse,
    responses=problems(
        ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_CLASH, ErrorCode.MEDIA_FORMAT_EXISTS,
        ErrorCode.MEDIA_FORMAT_EXTENSION_CLASH, ErrorCode.MEDIA_FORMAT_SIZE_ABOVE_CEILING,
        *ADMIN,
    ),
)
async def create_media_format(
        request: Request,
        data: MediaFormatCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW, Permissions.MEDIA_FORMAT_CREATE], admin=True))
):
    """Add an accepted format. Creating an archived content_type again restores it with the new values."""
    current_user = auth.user
    _check_size(data.max_file_size_bytes)

    existing = (await session.exec(select(MediaFormat).where(MediaFormat.content_type == data.content_type))).first()
    if existing and not existing.is_deleted:
        raise ApiError(ErrorCode.MEDIA_FORMAT_EXISTS, content_type=data.content_type, format_id=existing.id)
    await _check_conflicts(session, data.media_type, data.extensions, data.aliases, data.content_type,
                           exclude_id=existing.id if existing else None)

    if existing:
        old_values = existing.model_dump()
        for field_name, value in data.model_dump().items():
            setattr(existing, field_name, value)
        existing.is_deleted = False
        existing.updated_at = get_default_timezone_datetime()
        row = existing
        action = "restore"
    else:
        row = MediaFormat(**data.model_dump())
        old_values = None
        action = "create"

    session.add(row)
    await _commit_content_type(session, data.content_type)
    await session.refresh(row)
    invalidate_registry_cache()

    await create_audit_log(
        session=session, action=action, resource_type="media_format", resource_id=row.id,
        user_id=current_user.id, old_values=old_values, new_values=row.model_dump(),
        details={"message": f"User {current_user.username} {action}d media format '{row.content_type}'"},
        **client_info(request, auth),
    )
    await session.commit()
    return MediaFormatResponse.model_validate(row)


@router.get(
    MediaFormatRoutes.FORMAT_BY_ID.value,
    response_model=MediaFormatResponse,
    responses=problems(ErrorCode.MEDIA_FORMAT_NOT_FOUND, *GUARDED),
)
async def get_media_format(
        format_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW]))
):
    """One format; archived rows are visible to holders of media_format.archive so they can be restored."""
    row = await _get_format(session, format_id, include_deleted=auth.has(Permissions.MEDIA_FORMAT_ARCHIVE))
    return MediaFormatResponse.model_validate(row)


@router.put(
    MediaFormatRoutes.FORMAT_BY_ID.value,
    response_model=MediaFormatResponse,
    responses=problems(
        ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_CLASH, ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_MISMATCH,
        ErrorCode.MEDIA_FORMAT_EXISTS, ErrorCode.MEDIA_FORMAT_EXTENSION_CLASH, ErrorCode.MEDIA_FORMAT_NO_FIELDS,
        ErrorCode.MEDIA_FORMAT_SIZE_ABOVE_CEILING, ErrorCode.MEDIA_FORMAT_NOT_FOUND,
        *ADMIN,
    ),
)
async def update_media_format(
        request: Request,
        format_id: int,
        data: MediaFormatUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW, Permissions.MEDIA_FORMAT_UPDATE], admin=True))
):
    """Partial update. Archived formats answer 404 until restored (POST /{format_id}/restore)."""
    current_user = auth.user
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise ApiError(ErrorCode.MEDIA_FORMAT_NO_FIELDS)

    row = await _get_format(session, format_id)
    old_values = row.model_dump()

    media_type = changes.get("media_type", row.media_type)
    content_type = changes.get("content_type", row.content_type)
    if not content_type.startswith(f"{media_type}/"):
        raise ApiError(ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_MISMATCH, content_type=content_type, media_type=media_type)
    if "max_file_size_bytes" in changes:
        _check_size(changes["max_file_size_bytes"])
    if "content_type" in changes and content_type != row.content_type:
        other = (await session.exec(select(MediaFormat).where(MediaFormat.content_type == content_type))).first()
        if other:
            raise ApiError(ErrorCode.MEDIA_FORMAT_EXISTS, content_type=content_type, format_id=other.id)
    await _check_conflicts(
        session, media_type,
        changes.get("extensions", row.extensions), changes.get("aliases", row.aliases), content_type,
        exclude_id=row.id,
    )

    for field_name, value in changes.items():
        setattr(row, field_name, value)
    row.updated_at = get_default_timezone_datetime()
    session.add(row)
    await _commit_content_type(session, content_type)
    await session.refresh(row)
    invalidate_registry_cache()

    await create_audit_log(
        session=session, action="update", resource_type="media_format", resource_id=row.id,
        user_id=current_user.id, old_values=old_values, new_values=row.model_dump(),
        details={"message": f"User {current_user.username} updated media format '{row.content_type}'"},
        **client_info(request, auth),
    )
    await session.commit()
    return MediaFormatResponse.model_validate(row)


@router.post(
    MediaFormatRoutes.FORMAT_ARCHIVE.value,
    response_model=MediaFormatResponse,
    responses=problems(ErrorCode.MEDIA_FORMAT_NOT_FOUND, *ADMIN),
)
async def archive_media_format(
        request: Request,
        format_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW, Permissions.MEDIA_FORMAT_ARCHIVE], admin=True))
):
    """Archive a format (is_deleted=true): it stops being accepted immediately, uploaded files are untouched.
    Undo with POST /{format_id}/restore or by creating the same content_type again. Already archived or unknown: 404."""
    current_user = auth.user
    row = await _get_format(session, format_id)
    old_values = row.model_dump()
    row.is_deleted = True
    row.updated_at = get_default_timezone_datetime()
    session.add(row)
    await create_audit_log(
        session=session, action="archive", resource_type="media_format", resource_id=row.id,
        user_id=current_user.id, old_values=old_values, new_values=row.model_dump(),
        details={"message": f"User {current_user.username} archived media format '{row.content_type}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    invalidate_registry_cache()
    return MediaFormatResponse.model_validate(row)


@router.post(
    MediaFormatRoutes.FORMAT_RESTORE.value,
    response_model=MediaFormatResponse,
    responses=problems(
        ErrorCode.MEDIA_FORMAT_CONTENT_TYPE_CLASH, ErrorCode.MEDIA_FORMAT_EXTENSION_CLASH,
        ErrorCode.MEDIA_FORMAT_NOT_FOUND, ErrorCode.MEDIA_FORMAT_NOT_ARCHIVED, *ADMIN,
    ),
)
async def restore_media_format(
        request: Request,
        format_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW, Permissions.MEDIA_FORMAT_ARCHIVE], admin=True))
):
    """Restore an archived format (is_deleted=false): it is accepted again. Its extensions and aliases must not
    collide with a live format (400). Not archived: 409."""
    current_user = auth.user
    row = await _get_format(session, format_id, include_deleted=True)
    if not row.is_deleted:
        raise ApiError(ErrorCode.MEDIA_FORMAT_NOT_ARCHIVED)
    await _check_conflicts(session, row.media_type, row.extensions, row.aliases, row.content_type, exclude_id=row.id)

    old_values = row.model_dump()
    row.is_deleted = False
    row.updated_at = get_default_timezone_datetime()
    session.add(row)
    await create_audit_log(
        session=session, action="restore", resource_type="media_format", resource_id=row.id,
        user_id=current_user.id, old_values=old_values, new_values=row.model_dump(),
        details={"message": f"User {current_user.username} restored media format '{row.content_type}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    invalidate_registry_cache()
    return MediaFormatResponse.model_validate(row)
