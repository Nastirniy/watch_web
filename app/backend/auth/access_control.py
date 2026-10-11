from typing import Optional, FrozenSet
from loguru import logger

from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.errors import AccessErrorCode
from app.backend.models.user import User
from app.backend.models.observation import Observation
from app.backend.models.media import MediaFile
from app.backend.auth.context import AuthContext
from app.backend.auth.access_errors import access_denied
from app.backend.auth.require import Require


class AccessControlService:
    """Data scope and ownership checks. All checks read the per-request AuthContext
    and never query the database.

    Scope rules:
    - post.access_all: every post is readable and writable
    - otherwise: home post (users.post_id) is readable and writable, rows in
      user_post_access are readable, and writable when can_write is set
    - an archived post is in no home-post or access-row scope (auth/context.py, scope_post_rows); its
      data is read-only, writes answer 409 post_archived (api/shared/helpers/post_helper.py)
    - admin.true plays no role here; it only gates administration routes
    """

    # ------------------------------------------------------------------ scope
    @staticmethod
    def post_scope(auth: AuthContext) -> Optional[FrozenSet[int]]:
        """Post ids to filter list queries by. None means unrestricted."""
        return auth.post_scope()

    @staticmethod
    def post_record_scope(auth: AuthContext) -> Optional[FrozenSet[int]]:
        """Scope for reading post records themselves (name, code, status), not the data of a post.
        Administrators (admin.true, the route already requires post.view) see every post record;
        everyone else sees the posts of their data scope. None means unrestricted."""
        return None if auth.is_admin else auth.post_scope()

    @staticmethod
    def resolve_record_scope(auth: AuthContext, requested_post_id: Optional[int]) -> Optional[FrozenSet[int]]:
        """Like resolve_post_scope, for lists of post and user records (not post data).
        Administrators (admin.true, the route already requires the X.view permission) are not limited
        to their data scope: the requested post, or every post when none is requested."""
        if auth.is_admin:
            return None if requested_post_id is None else frozenset({requested_post_id})
        return AccessControlService.resolve_post_scope(auth, requested_post_id)

    @staticmethod
    def check_post_record_view(auth: AuthContext, post_id: int) -> None:
        """Single post record: administrators see any, everyone else needs the post in their data scope"""
        if auth.is_admin:
            return
        AccessControlService.check_post_view(auth, post_id, "post")

    @staticmethod
    def resolve_post_scope(auth: AuthContext, requested_post_id: Optional[int]) -> Optional[FrozenSet[int]]:
        """Scope for list endpoints that accept a post_id filter.

        - nothing requested: the caller's whole scope (None = unrestricted)
        - requested post inside the scope: only that post
        - requested post outside the scope: 403 post_access_denied
        """
        if requested_post_id is None:
            return auth.post_scope()
        AccessControlService.check_post_view(auth, requested_post_id)
        return frozenset({requested_post_id})

    @staticmethod
    def check_post_view(auth: AuthContext, post_id: int, resource_type: str = "post") -> None:
        if auth.can_view_post(post_id):
            return
        if not auth.readable_post_ids:
            logger.warning(f"User {auth.user.username} has no post assigned, tried to view {resource_type} of post {post_id}")
            raise access_denied(AccessErrorCode.NO_POST_ASSIGNED)
        logger.warning(f"User {auth.user.username} tried to view {resource_type} of post {post_id} outside of scope")
        raise access_denied(AccessErrorCode.POST_ACCESS_DENIED, resource_type=resource_type, post_id=post_id)

    @staticmethod
    def check_post_write(auth: AuthContext, post_id: int, resource_type: str = "post") -> None:
        AccessControlService.check_post_view(auth, post_id, resource_type)
        if auth.can_write_post(post_id):
            return
        logger.warning(f"User {auth.user.username} has read-only access to post {post_id}, tried to write {resource_type}")
        raise access_denied(AccessErrorCode.POST_WRITE_DENIED, post_id=post_id, resource_type=resource_type)

    # ------------------------------------------------------------ permissions
    @staticmethod
    def check_permission(auth: AuthContext, permission: Permissions, action: str) -> None:
        """Explicit permission check inside a handler (for optional request features such as include_deleted)."""
        if auth.has(permission):
            return
        logger.warning(f"User {auth.user.username} lacks {permission.value} for {action}")
        raise access_denied(
            AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
            required=Require.of(all=[permission]),
            missing=[permission.value],
            permission=permission.value,
            action=action,
        )

    @staticmethod
    def check_include_deleted(auth: AuthContext, params, permission: Permissions) -> None:
        """include_deleted (archived rows too) on a list or search requires the matching X.archive permission."""
        if getattr(params, "include_deleted", False):
            AccessControlService.check_permission(auth, permission, "include_deleted")

    # -------------------------------------------------------------- ownership
    @staticmethod
    def check_observation_ownership(auth: AuthContext, observation: Observation) -> None:
        """Owner, or observation.manage_any"""
        if observation.user_id == auth.user.id or auth.has(Permissions.OBSERVATION_MANAGE_ANY):
            return
        logger.warning(f"User {auth.user.username} is not the owner of observation {observation.id}")
        raise access_denied(
            AccessErrorCode.OBSERVATION_NOT_OWNER,
            required=Require.of(all=[Permissions.OBSERVATION_MANAGE_ANY]),
            missing=[Permissions.OBSERVATION_MANAGE_ANY.value],
        )

    @staticmethod
    def check_media_ownership(auth: AuthContext, media_file: MediaFile, observation: Optional[Observation]) -> None:
        """media.manage_any bypasses everything. Otherwise the observation must be
        active (when attached) and the caller must be the uploader."""
        if auth.has(Permissions.MEDIA_MANAGE_ANY):
            return
        if observation is not None and observation.status != ObservationStatus.ACTIVE.value:
            logger.warning(f"User {auth.user.username} tried to archive/restore media {media_file.id} of a non-active observation")
            raise access_denied(
                AccessErrorCode.OBSERVATION_NOT_ACTIVE,
                required=Require.of(all=[Permissions.MEDIA_MANAGE_ANY]),
                missing=[Permissions.MEDIA_MANAGE_ANY.value],
                observation_status=observation.status,
            )
        if media_file.user_id != auth.user.id:
            logger.warning(f"User {auth.user.username} is not the uploader of media {media_file.id}")
            raise access_denied(
                AccessErrorCode.MEDIA_NOT_OWNER,
                required=Require.of(all=[Permissions.MEDIA_MANAGE_ANY]),
                missing=[Permissions.MEDIA_MANAGE_ANY.value],
            )

    @staticmethod
    def check_user_visibility(auth: AuthContext, target: User) -> None:
        """A user record is visible to the user itself, to administrators (admin.true with user.view on the route),
        to post.access_all (like the list, users without a post included) and to callers whose post scope covers the target's post"""
        if target.id == auth.user.id or auth.is_admin or auth.has_post_access_all:
            return
        if target.post_id is not None and auth.can_view_post(target.post_id):
            return
        logger.warning(f"User {auth.user.username} tried to view user {target.id} outside of scope")
        raise access_denied(AccessErrorCode.USER_SELF_ONLY)
