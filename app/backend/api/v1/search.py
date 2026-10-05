"""
Unified Search API - Strongly typed endpoints for each entity
All params visible in FastAPI /docs
"""

from typing import Dict, Annotated
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.auth_context import AuthContext
from app.backend.services.access_control import AccessControlService
from app.backend.enums.routes import SearchRoutes
from app.backend.enums.general import Permissions
from app.backend.services.permissions import require
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.observation import Observation, Category, SignalType, Tag
from app.backend.models.media_format import MediaFormat
from app.backend.models.media import MediaFile
from app.backend.helpers.audit_helper import client_info, create_audit_log

# Filter params
from app.backend.api.v1.models.query.observations import ObservationFilterParams
from app.backend.api.v1.models.query.users import UserFilterParams
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.api.v1.models.query.roles import RoleFilterParams
from app.backend.api.v1.models.query.categories import CategoryFilterParams
from app.backend.api.v1.models.query.media_formats import MediaFormatFilterParams
from app.backend.api.v1.models.query.signal_types import SignalTypeFilterParams
from app.backend.api.v1.models.query.tags import TagFilterParams
from app.backend.api.v1.models.query.media import MediaFilterParams

# Filter services
from app.backend.filters.observation_filter import ObservationFilterService
from app.backend.filters.user_filter import UserFilterService
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.role_filter import RoleFilterService
from app.backend.filters.category_filter import CategoryFilterService
from app.backend.filters.media_format_filter import MediaFormatFilterService
from app.backend.filters.signal_type_filter import SignalTypeFilterService
from app.backend.filters.tag_filter import TagFilterService
from app.backend.filters.media_filter import MediaFilterService

from app.backend.api.v1.models.response.observations import ObservationWithMedia
from app.backend.api.v1.models.response.posts import PostResponse, post_to_response
from app.backend.api.v1.models.response.users import UserResponse
from app.backend.api.v1.models.response.roles import RoleResponse
from app.backend.api.v1.models.response.categories import CategoryResponse
from app.backend.api.v1.models.response.signal_types import SignalTypeResponse
from app.backend.api.v1.models.response.tags import TagResponse
from app.backend.api.v1.models.response.media import MediaFileInfo
from app.backend.api.v1.models.response.media_formats import MediaFormatResponse
from app.backend.api.v1.models.response.search import SearchResponse
from app.backend.openapi_problems import ADMIN, GUARDED, problems
from app.backend.enums.errors import AccessErrorCode


router = APIRouter(prefix=SearchRoutes.BASE.value, tags=[SearchRoutes.tag()])


async def log_search(
    request: Request,
    session: AsyncSession,
    auth: AuthContext,
    entity: str,
    params: Dict,
    results_count: int
) -> None:
    """Log search to audit; like every audit entry, a failure to write it fails the request"""
    await create_audit_log(
        session=session,
        user_id=auth.user.id,
        action="search",
        resource_type=entity,
        new_values={"params": params, "results": results_count},
        details={"message": f"User {auth.user.username} searched for {entity} with {results_count} results"},
        success=True,
        **client_info(request, auth),
    )
    await session.commit()


# =============================================================================
# OBSERVATIONS
# =============================================================================
@router.get(
    SearchRoutes.OBSERVATIONS.value,
    response_model=SearchResponse[ObservationWithMedia],
    summary="Search observations",
    description="Search observations with typed filters. All params visible in docs.",
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.POST_ACCESS_DENIED, *GUARDED,
    ),
)
async def search_observations(
    request: Request,
    params: Annotated[ObservationFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[ObservationWithMedia]:
    access_post_ids = AccessControlService.resolve_post_scope(auth, params.post_id)

    AccessControlService.check_include_deleted(auth, params, Permissions.OBSERVATION_ARCHIVE)
    query = select(Observation)
    query = ObservationFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    # Convert database objects to response models automatically
    processed_items = [ObservationWithMedia.model_validate(obs) for obs in items]

    count_query = select(func.count()).select_from(Observation)
    count_query = ObservationFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "observations", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[ObservationWithMedia].create("observations", processed_items, total, params.page, params.page_size)


# =============================================================================
# USERS
# =============================================================================
@router.get(
    SearchRoutes.USERS.value,
    response_model=SearchResponse[UserResponse],
    summary="Search users",
    description="Search users with typed filters.",
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.POST_ACCESS_DENIED, *GUARDED,
    ),
)
async def search_users(
    request: Request,
    params: Annotated[UserFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[UserResponse]:
    access_post_ids = AccessControlService.resolve_record_scope(auth, params.post_id)

    AccessControlService.check_include_deleted(auth, params, Permissions.USER_ARCHIVE)
    query = select(User)
    query = UserFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(User)
    count_query = UserFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "users", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[UserResponse].create("users", [UserResponse.model_validate(item) for item in items], total, params.page, params.page_size)


# =============================================================================
# POSTS
# =============================================================================
@router.get(
    SearchRoutes.POSTS.value,
    response_model=SearchResponse[PostResponse],
    summary="Search posts",
    description="Search posts with typed filters.",
    responses=problems(*GUARDED),
)
async def search_posts(
    request: Request,
    params: Annotated[PostFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[PostResponse]:
    access_post_ids = AccessControlService.post_record_scope(auth)

    query = select(Post)
    query = PostFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Post)
    count_query = PostFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "posts", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[PostResponse].create("posts", [post_to_response(post) for post in items], total, params.page, params.page_size)


# =============================================================================
# ROLES
# =============================================================================
@router.get(
    SearchRoutes.ROLES.value,
    response_model=SearchResponse[RoleResponse],
    summary="Search roles",
    description="Search roles with typed filters.",
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *ADMIN),
)
async def search_roles(
    request: Request,
    params: Annotated[RoleFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.ROLE_VIEW], admin=True)),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[RoleResponse]:
    AccessControlService.check_include_deleted(auth, params, Permissions.ROLE_ARCHIVE)
    query = select(Role)
    query = RoleFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Role)
    count_query = RoleFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "roles", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[RoleResponse].create("roles", [RoleResponse.model_validate(item) for item in items], total, params.page, params.page_size)


# =============================================================================
# CATEGORIES
# =============================================================================
@router.get(
    SearchRoutes.CATEGORIES.value,
    response_model=SearchResponse[CategoryResponse],
    summary="Search categories",
    description="Search categories with typed filters.",
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def search_categories(
    request: Request,
    params: Annotated[CategoryFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[CategoryResponse]:
    AccessControlService.check_include_deleted(auth, params, Permissions.CATEGORY_ARCHIVE)
    query = select(Category)
    query = CategoryFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Category)
    count_query = CategoryFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "categories", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[CategoryResponse].create("categories", [CategoryResponse.model_validate(item) for item in items], total, params.page, params.page_size)


# =============================================================================
# SIGNAL TYPES
# =============================================================================
@router.get(
    SearchRoutes.SIGNAL_TYPES.value,
    response_model=SearchResponse[SignalTypeResponse],
    summary="Search signal types",
    description="Search signal types with typed filters.",
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def search_signal_types(
    request: Request,
    params: Annotated[SignalTypeFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[SignalTypeResponse]:
    AccessControlService.check_include_deleted(auth, params, Permissions.SIGNAL_TYPE_ARCHIVE)
    query = select(SignalType)
    query = SignalTypeFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(SignalType)
    count_query = SignalTypeFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "signal_types", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[SignalTypeResponse].create("signal_types", [SignalTypeResponse.model_validate(item) for item in items], total, params.page, params.page_size)


# =============================================================================
# TAGS
# =============================================================================
@router.get(
    SearchRoutes.TAGS.value,
    response_model=SearchResponse[TagResponse],
    summary="Search tags",
    description="Search tags with typed filters.",
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def search_tags(
    request: Request,
    params: Annotated[TagFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[TagResponse]:
    AccessControlService.check_include_deleted(auth, params, Permissions.TAG_ARCHIVE)
    query = select(Tag)
    query = TagFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Tag)
    count_query = TagFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "tags", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[TagResponse].create("tags", [TagResponse.model_validate(item) for item in items], total, params.page, params.page_size)


# =============================================================================
# MEDIA
# =============================================================================
@router.get(
    SearchRoutes.MEDIA.value,
    response_model=SearchResponse[MediaFileInfo],
    summary="Search media files",
    description="Search media files with typed filters.",
    responses=problems(*GUARDED),
)
async def search_media(
    request: Request,
    params: Annotated[MediaFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[MediaFileInfo]:
    access_post_ids = AccessControlService.post_scope(auth)

    query = select(MediaFile)
    query = MediaFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(MediaFile)
    count_query = MediaFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "media", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[MediaFileInfo].create("media", [MediaFileInfo.model_validate(item) for item in items], total, params.page, params.page_size)


# =============================================================================
# MEDIA FORMATS (accepted upload formats)
# =============================================================================

@router.get(
    SearchRoutes.MEDIA_FORMATS.value,
    response_model=SearchResponse[MediaFormatResponse],
    summary="Search media formats",
    description="Search accepted upload formats with typed filters.",
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def search_media_formats(
    request: Request,
    params: Annotated[MediaFormatFilterParams, Query()],
    auth: AuthContext = Depends(require(all=[Permissions.MEDIA_FORMAT_VIEW])),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse[MediaFormatResponse]:
    AccessControlService.check_include_deleted(auth, params, Permissions.MEDIA_FORMAT_ARCHIVE)
    query = select(MediaFormat)
    query = MediaFormatFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(MediaFormat)
    count_query = MediaFormatFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, auth, "media_formats", params.model_dump(exclude_none=True), len(items))

    return SearchResponse[MediaFormatResponse].create("media_formats", [MediaFormatResponse.model_validate(item) for item in items], total, params.page, params.page_size)
