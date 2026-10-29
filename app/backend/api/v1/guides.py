"""User guides: the help section, titled entries with their files (services/guides.py).

Reading needs only an authenticated user (require() with no permission): there is no guide.view, so the rule that a
write route requires X.view as well does not apply here, reading is open. Writes need the system permissions
(held through a system role): create guide.create, PATCH guide.update, archive and restore guide.archive.
include_deleted (archived guides and files too) requires guide.archive.

The audience ({kind: post | group, id}, null: everyone) is stored as the scope key of the node; an unknown or
archived node answers 404 post_not_found / scope_group_not_found, as the assignment API does. Every live guide is
listed for every user for now; filtering by audience comes with part 2, task 7.

Nothing is removed: POST /{guide_id}/archive hides a guide (is_deleted=true), POST /{guide_id}/restore brings it
back. An archived guide answers 404 on update, a live one 409 on restore. Every write and its audit row
(user_guide.*, system-wide: no scope) are one transaction.
"""
from typing import Annotated, List

from fastapi import APIRouter, Depends, Query, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.v1.models.query.guides import GuideFilterParams, GuideReadParams
from app.backend.api.v1.models.request.guides import GuideCreate, GuideUpdate
from app.backend.api.v1.models.response.guides import GuideResponse
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.database import get_session
from app.backend.enums.audit import AuditEvent
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.routes import GuideRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.models.user_guide import UserGuide
from app.backend.services import guides as guides_service
from app.backend.services.audit import create_audit_log
from app.backend.utils.datetime_utils import get_default_timezone_datetime

router = APIRouter(prefix=GuideRoutes.BASE.value, tags=[GuideRoutes.tag()])


def _response(view: guides_service.GuideView) -> GuideResponse:
    return GuideResponse.model_validate(view, from_attributes=True)


async def _guide_or_404(session: AsyncSession, guide_id: int, include_deleted: bool = False,
                        lock: bool = False) -> UserGuide:
    try:
        return await guides_service.get_guide(session, guide_id, include_deleted, lock=lock)
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
):
    """The guides with their files, by sort_order then title. Archived guides and files only with include_deleted
    (requires guide.archive)."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.GUIDE_ARCHIVE)
    views = await guides_service.list_guides(session, guides_service.GuideFilterService.criteria(filters))
    return [_response(view) for view in views]


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
    await session.commit()
    await session.refresh(guide)
    return _response(await guides_service.guide_view(session, guide))


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
):
    """One guide with its files. An archived guide only with include_deleted (requires guide.archive), which also
    lists its archived files; otherwise 404."""
    AccessControlService.check_include_deleted(auth, params, Permissions.GUIDE_ARCHIVE)
    guide = await _guide_or_404(session, guide_id, include_deleted=params.include_deleted)
    return _response(await guides_service.guide_view(session, guide, include_deleted_files=params.include_deleted))


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
):
    """Change the fields sent. description: null clears it; audience: null makes the guide for everyone, left out
    keeps it (an unknown or archived node: 404). An archived guide: 404 (restore it first)."""
    guide = await _guide_or_404(session, guide_id, lock=True)
    sent = data.model_fields_set
    changes = {field: getattr(data, field) for field in ("title", "description", "sort_order") if field in sent}
    if "audience" in sent:
        changes["audience_key"] = await _audience_key(session, data.audience) if data.audience is not None else None
    if not changes:     # an empty body changes nothing and writes no audit row
        return _response(await guides_service.guide_view(session, guide))

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
    await session.commit()
    await session.refresh(guide)
    return _response(await guides_service.guide_view(session, guide))


async def _set_archived(request: Request, session: AsyncSession, auth: AuthContext, guide: UserGuide,
                        archived: bool) -> GuideResponse:
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
    await session.commit()
    await session.refresh(guide)
    return _response(await guides_service.guide_view(session, guide))


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
):
    """Archive a guide (is_deleted=true). Nothing is removed: its files stay, the guide is hidden from lists unless
    include_deleted=true. Undo with POST /{guide_id}/restore. Already archived or unknown: 404."""
    guide = await _guide_or_404(session, guide_id, lock=True)
    return await _set_archived(request, session, auth, guide, archived=True)


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
):
    """Restore an archived guide (is_deleted=false). Not archived: 409."""
    guide = await _guide_or_404(session, guide_id, include_deleted=True, lock=True)
    if not guide.is_deleted:
        raise ApiError(ErrorCode.GUIDE_NOT_ARCHIVED)
    return await _set_archived(request, session, auth, guide, archived=False)
