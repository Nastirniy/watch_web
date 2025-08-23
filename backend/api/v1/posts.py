"""
Post management API routes for monitoring stations and locations.
Handles post CRUD operations, status management, and location services.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger

from backend.core.exceptions import (
    ResourceConflictException,
    ResourceNotFoundException,
)
from backend.core.permissions import SystemPermissions
from backend.database.crud.audit import audit_log as audit_crud
from backend.database.crud.post import post as post_crud
from backend.routes.dependencies import DatabaseDep, CurrentUser, OptionalUser, require_permissions
from backend.database.models import AuditAction, AuditSeverity, ResourceType
from backend.database.models import ConnectionStatus
from backend.api.schemas import (
    BulkOperationResponse,
    MessageResponse,
    PaginatedResponse,
    create_paginated_response,
)
from backend.api.schemas.post import (
    BulkStatusUpdate,
    ConnectionStatusUpdate,
    LocationSearch,
    Post,
    PostActivity,
    PostCreate,
    PostSearchFilters,
    PostStatistics,
    PostSummary,
    PostUpdate,
    RegionSummary,
)

router = APIRouter()


@router.post("", response_model=Post, status_code=status.HTTP_201_CREATED)
async def create_post(
        post_data: PostCreate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_CREATE)),
) -> Post:
    """
    Create a new monitoring post.

    Args:
        post_data: Post creation data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created post

    Raises:
        ResourceConflictException: If post code already exists
    """
    try:
        # Check if code already exists
        existing_post = await post_crud.get_by_code(db, post_data.code)
        if existing_post:
            raise ResourceConflictException(
                detail=f"Post with code '{post_data.code}' already exists",
                field="code"
            )

        # Create post
        post = await post_crud.create(db, post_data.model_dump())

        # Log creation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.CREATE,
            resource_type=ResourceType.POSTS,
            user_id=current_user.id,
            resource_id=post.id,
            resource_name=post.name,
            new_values=post_data.model_dump(),
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Post {post.name} created by {current_user.username}")
        return post

    except ResourceConflictException:
        raise
    except Exception as e:
        logger.error(f"Error creating post: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create post"
        )


@router.get("", response_model=PaginatedResponse[PostSummary])
async def list_posts(
        db: DatabaseDep,
        filters: PostSearchFilters = Depends(),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> PaginatedResponse[PostSummary]:
    """
    List posts with filtering and pagination.

    Args:
        filters: Search filters
        db: Database session
        current_user: Current authenticated user

    Returns:
        Paginated list of posts
    """
    try:
        # Build filter dictionary
        filter_dict = {}
        if filters.region:
            filter_dict["region"] = filters.region
        if filters.connection_status:
            filter_dict["connection_status"] = filters.connection_status
        if filters.is_active is not None:
            filter_dict["is_active"] = filters.is_active

        # Get posts
        posts = await post_crud.get_multi(
            db=db,
            skip=(filters.page - 1) * filters.per_page,
            limit=filters.per_page,
            filters=filter_dict,
            order_by=f"{'-' if filters.sort_order == 'desc' else ''}{filters.sort_by or 'name'}",
        )

        # Get total count
        total = await post_crud.count(db, filter_dict)

        # Convert to summary format
        post_summaries = []
        for post in posts:
            # Get observation count for last 30 days (simplified)
            obs_count = 0  # Would need additional query implementation

            post_summaries.append(PostSummary(
                id=post.id,
                name=post.name,
                code=post.code,
                region=post.region,
                is_active=post.is_active,
                connection_status=post.connection_status,
                last_connection_at=post.last_connection_at,
                user_count=0,  # Would need additional query
                observation_count=obs_count,
            ))

        return create_paginated_response(
            items=post_summaries,
            total=total,
            page=filters.page,
            per_page=filters.per_page,
        )

    except Exception as e:
        logger.error(f"Error listing posts: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve posts"
        )


@router.get("/active", response_model=List[PostSummary])
async def list_active_posts(
        db: DatabaseDep,
        current_user: OptionalUser,
) -> List[PostSummary]:
    """
    Get all active posts (public endpoint for operators).

    Args:
        db: Database session
        current_user: Optional current user

    Returns:
        List of active posts
    """
    try:
        posts = await post_crud.get_active_posts(db)

        post_summaries = []
        for post in posts:
            post_summaries.append(PostSummary(
                id=post.id,
                name=post.name,
                code=post.code,
                region=post.region,
                is_active=post.is_active,
                connection_status=post.connection_status,
                last_connection_at=post.last_connection_at,
                user_count=0,
                observation_count=0,
            ))

        return post_summaries

    except Exception as e:
        logger.error(f"Error listing active posts: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve active posts"
        )


@router.get("/{post_id}", response_model=Post)
async def get_post(
        post_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> Post:
    """
    Get post by ID.

    Args:
        post_id: Post ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Post details
    """
    post = await post_crud.get(db, post_id)
    if not post:
        raise ResourceNotFoundException("Post", post_id)

    return post


@router.put("/{post_id}", response_model=Post)
async def update_post(
        post_id: int,
        update_data: PostUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_UPDATE)),
) -> Post:
    """
    Update post.

    Args:
        post_id: Post ID
        update_data: Update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated post
    """
    # Get existing post
    post = await post_crud.get(db, post_id)
    if not post:
        raise ResourceNotFoundException("Post", post_id)

    try:
        # Store old values for audit
        old_values = {
            "name": post.name,
            "description": post.description,
            "region": post.region,
            "is_active": post.is_active,
        }

        # Update post
        updated_post = await post_crud.update(
            db, post, update_data.model_dump(exclude_unset=True)
        )

        # Log update
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.POSTS,
            user_id=current_user.id,
            resource_id=post_id,
            resource_name=updated_post.name,
            old_values=old_values,
            new_values=update_data.model_dump(exclude_unset=True),
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Post {updated_post.name} updated by {current_user.username}")
        return updated_post

    except Exception as e:
        logger.error(f"Error updating post {post_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update post"
        )


@router.delete("/{post_id}", response_model=MessageResponse)
async def delete_post(
        post_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_DELETE)),
) -> MessageResponse:
    """
    Delete post.

    Args:
        post_id: Post ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Deletion confirmation
    """
    # Get post
    post = await post_crud.get(db, post_id)
    if not post:
        raise ResourceNotFoundException("Post", post_id)

    try:
        # Check if post has observations
        # Note: In a real implementation, you might want to prevent deletion
        # of posts with observations or implement cascade rules

        # Delete post
        await post_crud.delete(db, post_id)

        # Log deletion
        await audit_crud.create_log(
            db=db,
            action=AuditAction.DELETE,
            resource_type=ResourceType.POSTS,
            user_id=current_user.id,
            resource_id=post_id,
            resource_name=post.name,
            old_values={"name": post.name, "code": post.code},
            success=True,
            severity=AuditSeverity.HIGH,
        )

        logger.info(f"Post {post.name} deleted by {current_user.username}")

        return MessageResponse(message="Post deleted successfully")

    except Exception as e:
        logger.error(f"Error deleting post {post_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete post"
        )


# Status Management
@router.put("/{post_id}/status", response_model=Post)
async def update_post_status(
        post_id: int,
        status_update: ConnectionStatusUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_UPDATE)),
) -> Post:
    """
    Update post connection status.

    Args:
        post_id: Post ID
        status_update: Status update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated post
    """
    try:
        post = await post_crud.update_connection_status(db, post_id, status_update.status)
        if not post:
            raise ResourceNotFoundException("Post", post_id)

        # Log status update
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.POSTS,
            user_id=current_user.id,
            resource_id=post_id,
            resource_name=post.name,
            new_values={"connection_status": status_update.status.value},
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Post {post.name} status updated to {status_update.status.value} by {current_user.username}")
        return post

    except ResourceNotFoundException:
        raise
    except Exception as e:
        logger.error(f"Error updating post status: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update post status"
        )


@router.post("/bulk/status-update", response_model=BulkOperationResponse)
async def bulk_update_post_status(
        bulk_update: BulkStatusUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_UPDATE)),
) -> BulkOperationResponse:
    """
    Bulk update post connection status.

    Args:
        bulk_update: Bulk status update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Bulk operation result
    """
    try:
        processed = len(bulk_update.post_ids)
        succeeded = 0
        errors = []

        for post_id in bulk_update.post_ids:
            try:
                post = await post_crud.update_connection_status(db, post_id, bulk_update.status)
                if post:
                    succeeded += 1
                else:
                    errors.append({
                        "post_id": post_id,
                        "error": "Post not found"
                    })
            except Exception as e:
                errors.append({
                    "post_id": post_id,
                    "error": str(e)
                })

        # Log bulk operation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.POSTS,
            user_id=current_user.id,
            resource_name="bulk_status_update",
            new_values={
                "post_ids": bulk_update.post_ids,
                "status": bulk_update.status.value,
                "reason": bulk_update.reason,
                "processed": processed,
                "succeeded": succeeded,
            },
            success=len(errors) == 0,
            severity=AuditSeverity.MEDIUM,
        )

        return BulkOperationResponse(
            message="Bulk status update completed",
            processed=processed,
            succeeded=succeeded,
            failed=processed - succeeded,
            errors=errors,
        )

    except Exception as e:
        logger.error(f"Error in bulk status update: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bulk status update failed"
        )


# Location Services
@router.post("/search/location", response_model=List[PostSummary])
async def search_posts_by_location(
        location_search: LocationSearch,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> List[PostSummary]:
    """
    Search posts by geographic location.

    Args:
        location_search: Location search parameters
        db: Database session
        current_user: Current authenticated user

    Returns:
        Posts within specified radius
    """
    try:
        posts = await post_crud.get_posts_by_location(
            db=db,
            latitude=location_search.latitude,
            longitude=location_search.longitude,
            radius_km=location_search.radius_km,
        )

        # Filter inactive posts if requested
        if not location_search.include_inactive:
            posts = [post for post in posts if post.is_active]

        # Convert to summary format
        post_summaries = []
        for post in posts:
            post_summaries.append(PostSummary(
                id=post.id,
                name=post.name,
                code=post.code,
                region=post.region,
                is_active=post.is_active,
                connection_status=post.connection_status,
                last_connection_at=post.last_connection_at,
                user_count=0,
                observation_count=0,
            ))

        return post_summaries

    except Exception as e:
        logger.error(f"Error searching posts by location: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search posts by location"
        )


# Statistics and Monitoring
@router.get("/statistics", response_model=PostStatistics)
async def get_post_statistics(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> PostStatistics:
    """
    Get post statistics.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Post statistics
    """
    try:
        # Get connection statistics
        connection_stats = await post_crud.get_connection_statistics(db)

        # Get basic counts
        total_posts = connection_stats.get("total", 0)
        active_posts = connection_stats.get("total_active", 0)

        # Count posts with recent activity
        recent_activity_posts = await post_crud.get_posts_with_recent_activity(db, hours=24)

        # Get region distribution
        regions = {}  # Would need additional implementation

        return PostStatistics(
            total_posts=total_posts,
            active_posts=active_posts,
            by_status=connection_stats,
            by_region=regions,
            with_users=0,  # Would need additional query
            with_recent_activity=len(recent_activity_posts),
            average_observations_per_post=0.0,  # Would need additional calculation
        )

    except Exception as e:
        logger.error(f"Error getting post statistics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve post statistics"
        )


@router.get("/by-status/{status}", response_model=List[PostSummary])
async def get_posts_by_status(
        status: ConnectionStatus,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> List[PostSummary]:
    """
    Get posts by connection status.

    Args:
        status: Connection status to filter by
        db: Database session
        current_user: Current authenticated user

    Returns:
        Posts with specified status
    """
    try:
        posts = await post_crud.get_by_connection_status(db, status)

        post_summaries = []
        for post in posts:
            post_summaries.append(PostSummary(
                id=post.id,
                name=post.name,
                code=post.code,
                region=post.region,
                is_active=post.is_active,
                connection_status=post.connection_status,
                last_connection_at=post.last_connection_at,
                user_count=0,
                observation_count=0,
            ))

        return post_summaries

    except Exception as e:
        logger.error(f"Error getting posts by status {status}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve posts by status"
        )


@router.get("/{post_id}/activity", response_model=PostActivity)
async def get_post_activity(
        post_id: int,
        db: DatabaseDep,
        days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> PostActivity:
    """
    Get post activity summary.

    Args:
        post_id: Post ID
        days: Number of days to analyze
        db: Database session
        current_user: Current authenticated user

    Returns:
        Post activity summary
    """
    try:
        # Get post with observation count
        result = await post_crud.get_with_observations_count(db, post_id, days)
        if not result:
            raise ResourceNotFoundException("Post", post_id)

        post, observation_count = result

        # Calculate additional metrics (simplified)
        uptime_percentage = None
        if post.connection_status == ConnectionStatus.ONLINE:
            uptime_percentage = 95.0  # Placeholder calculation

        return PostActivity(
            post_id=post.id,
            post_name=post.name,
            post_code=post.code,
            connection_status=post.connection_status,
            last_connection_at=post.last_connection_at,
            uptime_percentage=uptime_percentage,
            observation_count_24h=0,  # Would need additional queries
            observation_count_7d=0,
            observation_count_30d=observation_count,
            unique_signals_24h=0,
            last_observation_at=None,
            assigned_users=[],  # Would need additional query
        )

    except ResourceNotFoundException:
        raise
    except Exception as e:
        logger.error(f"Error getting post activity for {post_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve post activity"
        )


@router.get("/regions/summary", response_model=List[RegionSummary])
async def get_regional_summary(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_READ)),
) -> List[RegionSummary]:
    """
    Get summary statistics by region.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Regional summary statistics
    """
    try:
        # Get all posts grouped by region
        all_posts = await post_crud.get_multi(db, limit=1000)

        # Group by region
        regions = {}
        for post in all_posts:
            region = post.region or "Unknown"
            if region not in regions:
                regions[region] = {
                    "posts": [],
                    "active_count": 0,
                    "online_count": 0,
                }

            regions[region]["posts"].append(post)
            if post.is_active:
                regions[region]["active_count"] += 1
            if post.connection_status == ConnectionStatus.ONLINE:
                regions[region]["online_count"] += 1

        # Create summary for each region
        summaries = []
        for region_name, region_data in regions.items():
            summaries.append(RegionSummary(
                region=region_name,
                post_count=len(region_data["posts"]),
                active_post_count=region_data["active_count"],
                online_post_count=region_data["online_count"],
                total_observations=0,  # Would need additional query
                unique_signal_types=0,  # Would need additional query
                coverage_area_km2=None,  # Would need geographic calculation
                last_activity=None,  # Would need additional query
            ))

        return summaries

    except Exception as e:
        logger.error(f"Error getting regional summary: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve regional summary"
        )


# Maintenance Operations
@router.post("/maintenance/cleanup-offline", response_model=BulkOperationResponse)
async def cleanup_offline_posts(
        db: DatabaseDep,
        timeout_minutes: int = Query(60, ge=1, le=1440, description="Offline timeout in minutes"),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.POST_UPDATE)),
) -> BulkOperationResponse:
    """
    Mark posts as offline if they haven't connected recently.

    Args:
        timeout_minutes: Timeout in minutes
        db: Database session
        current_user: Current authenticated user

    Returns:
        Cleanup operation result
    """
    try:
        count = await post_crud.bulk_update_offline_status(db, timeout_minutes)

        # Log maintenance operation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.MAINTENANCE,
            resource_type=ResourceType.POSTS,
            user_id=current_user.id,
            resource_name="offline_cleanup",
            new_values={"timeout_minutes": timeout_minutes, "posts_updated": count},
            success=True,
            severity=AuditSeverity.LOW,
        )

        return BulkOperationResponse(
            message="Offline cleanup completed",
            processed=count,
            succeeded=count,
            failed=0,
        )

    except Exception as e:
        logger.error(f"Error in offline cleanup: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Offline cleanup failed"
        )
