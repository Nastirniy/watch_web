"""
Unified Search API - Strongly typed endpoints for each entity
All params visible in FastAPI /docs
"""

from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from loguru import logger

from app.backend.database import get_session
from app.backend.services.auth_context import AuthContext
from app.backend.services.access_control import AccessControlService
from app.backend.enums.routes import SearchRoutes
from app.backend.enums.general import Permissions
from app.backend.services.permissions import require, Require
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.observation import Observation, Category, SignalType, Tag
from app.backend.models.media_format import MediaFormat
from app.backend.models.media import MediaFile
from app.backend.models.audit import AuditLog
from app.backend.helpers.audit_helper import create_audit_log

# Filter params
from app.backend.filters.params.base_params import BaseFilterParams
from app.backend.filters.params.observation_params import ObservationFilterParams
from app.backend.filters.params.user_params import UserFilterParams
from app.backend.filters.params.post_params import PostFilterParams
from app.backend.filters.params.role_params import RoleFilterParams
from app.backend.filters.params.category_params import CategoryFilterParams
from app.backend.filters.params.media_format_params import MediaFormatFilterParams
from app.backend.filters.params.signal_type_params import SignalTypeFilterParams
from app.backend.filters.params.tag_params import TagFilterParams
from app.backend.filters.params.media_params import MediaFilterParams

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

from app.backend.filters.simple_engine import _get_type_name, _BASE_COMMON_FIELDS
from app.backend.api.shared.models.response.observations_response import ObservationWithMedia
from app.backend.api.shared.models.response.posts_response import post_to_response
from app.backend.api.shared.models.response.search_response import (
    SearchResponse,
    SchemaResponse,
    EntitySchemaInfo,
    FilterParamInfo,
)


router = APIRouter(prefix=SearchRoutes.BASE.value, tags=[SearchRoutes.tag()])

# Entity registry: name -> filter service and route requirement (shared by the search routes and the schema endpoint)
ENTITY_REGISTRY: Dict[str, Dict] = {
    "observations": {"service": ObservationFilterService, "require": Require.of(all=[Permissions.OBSERVATION_VIEW])},
    "users": {"service": UserFilterService, "require": Require.of(all=[Permissions.USER_VIEW])},
    "posts": {"service": PostFilterService, "require": Require.of(all=[Permissions.POST_VIEW])},
    "roles": {"service": RoleFilterService, "require": Require.of(all=[Permissions.ROLE_VIEW], admin=True)},
    "categories": {"service": CategoryFilterService, "require": Require.of(all=[Permissions.CATEGORY_VIEW])},
    "signal_types": {"service": SignalTypeFilterService, "require": Require.of(all=[Permissions.SIGNAL_TYPE_VIEW])},
    "tags": {"service": TagFilterService, "require": Require.of(all=[Permissions.TAG_VIEW])},
    "media": {"service": MediaFilterService, "require": Require.of(all=[Permissions.MEDIA_VIEW])},
    "media_formats": {"service": MediaFormatFilterService, "require": Require.of(all=[Permissions.MEDIA_FORMAT_VIEW])},
}



def _build_param_info(params_class, field_names: List[str]) -> List[FilterParamInfo]:
    """Build FilterParamInfo list from a params class for given field names"""
    result = []
    for name in field_names:
        field_info = params_class.model_fields.get(name)
        if field_info is None:
            continue
        description = field_info.description or ""
        type_str = _get_type_name(field_info.annotation)
        result.append(FilterParamInfo(name=name, type=type_str, description=description))
    return result


def get_client_ip(request: Request) -> str:
    """Extract client IP from request"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or (request.client.host if request.client else "unknown")


async def log_search(
    request: Request,
    session: AsyncSession,
    user: User,
    entity: str,
    params: Dict,
    results_count: int
) -> None:
    """Log search to audit"""
    try:
        ip = get_client_ip(request)
        await create_audit_log(
            session=session,
            user_id=user.id,
            session_id=getattr(request.state, "session_id", None),
            action="search",
            resource_type=entity,
            new_values={"params": params, "results": results_count},
            details={"message": f"User {user.username} searched for {entity} with {results_count} results"},
            ip_address=ip,
            request_url=request.url.path,
            success=True
        )
        await session.commit()
    except Exception as e:
        logger.error(f"Audit log failed: {e}")


# =============================================================================
# OBSERVATIONS
# =============================================================================
@router.get(
    SearchRoutes.OBSERVATIONS.value,
    response_model=SearchResponse,
    summary="Search observations",
    description="Search observations with typed filters. All params visible in docs."
)
async def search_observations(
    request: Request,
    params: ObservationFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["observations"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
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

    await log_search(request, session, current_user, "observations", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("observations", processed_items, total, params.page, params.page_size)


# =============================================================================
# USERS
# =============================================================================
@router.get(
    SearchRoutes.USERS.value,
    response_model=SearchResponse,
    summary="Search users",
    description="Search users with typed filters."
)
async def search_users(
    request: Request,
    params: UserFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["users"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    access_post_ids = AccessControlService.resolve_record_scope(auth, params.post_id)

    AccessControlService.check_include_deleted(auth, params, Permissions.USER_ARCHIVE)
    query = select(User)
    query = UserFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(User)
    count_query = UserFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "users", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("users", items, total, params.page, params.page_size)


# =============================================================================
# POSTS
# =============================================================================
@router.get(
    SearchRoutes.POSTS.value,
    response_model=SearchResponse,
    summary="Search posts",
    description="Search posts with typed filters."
)
async def search_posts(
    request: Request,
    params: PostFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["posts"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    access_post_ids = AccessControlService.post_record_scope(auth)

    query = select(Post)
    query = PostFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Post)
    count_query = PostFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "posts", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("posts", [post_to_response(post) for post in items], total, params.page, params.page_size)


# =============================================================================
# ROLES
# =============================================================================
@router.get(
    SearchRoutes.ROLES.value,
    response_model=SearchResponse,
    summary="Search roles",
    description="Search roles with typed filters."
)
async def search_roles(
    request: Request,
    params: RoleFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["roles"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    AccessControlService.check_include_deleted(auth, params, Permissions.ROLE_ARCHIVE)
    query = select(Role)
    query = RoleFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Role)
    count_query = RoleFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "roles", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("roles", items, total, params.page, params.page_size)


# =============================================================================
# CATEGORIES
# =============================================================================
@router.get(
    SearchRoutes.CATEGORIES.value,
    response_model=SearchResponse,
    summary="Search categories",
    description="Search categories with typed filters."
)
async def search_categories(
    request: Request,
    params: CategoryFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["categories"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    AccessControlService.check_include_deleted(auth, params, Permissions.CATEGORY_ARCHIVE)
    query = select(Category)
    query = CategoryFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Category)
    count_query = CategoryFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "categories", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("categories", items, total, params.page, params.page_size)


# =============================================================================
# SIGNAL TYPES
# =============================================================================
@router.get(
    SearchRoutes.SIGNAL_TYPES.value,
    response_model=SearchResponse,
    summary="Search signal types",
    description="Search signal types with typed filters."
)
async def search_signal_types(
    request: Request,
    params: SignalTypeFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["signal_types"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    AccessControlService.check_include_deleted(auth, params, Permissions.SIGNAL_TYPE_ARCHIVE)
    query = select(SignalType)
    query = SignalTypeFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(SignalType)
    count_query = SignalTypeFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "signal_types", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("signal_types", items, total, params.page, params.page_size)


# =============================================================================
# TAGS
# =============================================================================
@router.get(
    SearchRoutes.TAGS.value,
    response_model=SearchResponse,
    summary="Search tags",
    description="Search tags with typed filters."
)
async def search_tags(
    request: Request,
    params: TagFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["tags"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    AccessControlService.check_include_deleted(auth, params, Permissions.TAG_ARCHIVE)
    query = select(Tag)
    query = TagFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(Tag)
    count_query = TagFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "tags", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("tags", items, total, params.page, params.page_size)


# =============================================================================
# MEDIA
# =============================================================================
@router.get(
    SearchRoutes.MEDIA.value,
    response_model=SearchResponse,
    summary="Search media files",
    description="Search media files with typed filters."
)
async def search_media(
    request: Request,
    params: MediaFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["media"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    access_post_ids = AccessControlService.post_scope(auth)

    query = select(MediaFile)
    query = MediaFilterService.apply_filters(query, params, access_post_ids)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(MediaFile)
    count_query = MediaFilterService.apply_filters_for_count(count_query, params, access_post_ids)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "media", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("media", items, total, params.page, params.page_size)


# =============================================================================
# SCHEMA - Metadata about available entities, sortable fields, filter params
# =============================================================================
@router.get(
    SearchRoutes.FILTER_SCHEMA.value,
    response_model=SchemaResponse,
    summary="Get filter schema for all entities",
    description="Returns sortable fields and filter params for each searchable entity. "
                "Fields are filtered based on user access level."
)
async def get_search_filter_schema(
    auth: AuthContext = Depends(require())
) -> SchemaResponse:
    entities: Dict[str, EntitySchemaInfo] = {}

    for entity_name, config in ENTITY_REGISTRY.items():
        if not config["require"].evaluate(auth).allowed:
            continue
        entities[entity_name] = EntitySchemaInfo(**config["service"].get_schema(is_admin=auth.is_admin))

    common_params = _build_param_info(BaseFilterParams, list(_BASE_COMMON_FIELDS))

    return SchemaResponse(entities=entities, common_params=common_params)


# =============================================================================
# MEDIA FORMATS (accepted upload formats)
# =============================================================================

@router.get(
    SearchRoutes.MEDIA_FORMATS.value,
    response_model=SearchResponse,
    summary="Search media formats",
    description="Search accepted upload formats with typed filters."
)
async def search_media_formats(
    request: Request,
    params: MediaFormatFilterParams = Depends(),
    auth: AuthContext = Depends(ENTITY_REGISTRY["media_formats"]["require"]),
    session: AsyncSession = Depends(get_session)
) -> SearchResponse:
    current_user = auth.user
    AccessControlService.check_include_deleted(auth, params, Permissions.MEDIA_FORMAT_ARCHIVE)
    query = select(MediaFormat)
    query = MediaFormatFilterService.apply_filters(query, params)
    result = await session.exec(query)
    items = result.all()

    count_query = select(func.count()).select_from(MediaFormat)
    count_query = MediaFormatFilterService.apply_filters_for_count(count_query, params)
    total = (await session.exec(count_query)).one()

    await log_search(request, session, current_user, "media_formats", params.model_dump(exclude_none=True), len(items))

    return SearchResponse.create("media_formats", items, total, params.page, params.page_size)
