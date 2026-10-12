from typing import Optional, FrozenSet, Union
from loguru import logger

from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.errors import AccessErrorCode
from app.backend.models.user import User
from app.backend.models.observation import Observation
from app.backend.models.media import MediaFile
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node
from app.backend.auth.access_errors import access_denied
from app.backend.auth.require import Require


def _name(permission: Union[Permissions, str]) -> str:
    return permission.value if isinstance(permission, Permissions) else str(permission)


class AccessControlService:
    """Data scope and ownership checks. All checks read the per-request AuthContext
    and never query the database.

    Permission at the post (authorization v2, spec section 5): require() on the route checks that the permission is
    held somewhere; the handler checks where it applies with the permission itself:
    - scope_for / resolve_scope: the posts a list covers (posts_with: None = every post)
    - check_post: one post (403 post_access_denied for *.view, post_write_denied otherwise)
    - check_covers: where a post is created or moved to (403 permission_required_for_action)
    - check_user_view: one user record (the caller, or user.view covering the user's home post)
    - archived_scope: include_deleted needs X.archive held anywhere; archived rows only where it is in effect
    Grants on an archived post stop applying, grants inherited from a group or the root keep applying.

    Legacy scope (the routes not converted yet):
    - post.access_all: every post is readable and writable
    - otherwise: home post (users.post_id) is readable and writable, rows in
      user_post_access are readable, and writable when can_write is set
    - an archived post is in no home-post or access-row scope (auth/context.py, _legacy_data_scope); its
      data is read-only, writes answer 409 post_archived (api/shared/helpers/post_helper.py)
    - admin.true plays no role here; it only gates administration routes
    """

    # ------------------------------------------------------- permission at the post
    @staticmethod
    def scope_for(auth: AuthContext, permission: Union[Permissions, str]) -> Optional[FrozenSet[int]]:
        """Post ids where the permission is in effect, to filter a list by. None means every post."""
        return auth.posts_with(permission)

    @staticmethod
    def resolve_scope(
            auth: AuthContext, permission: Union[Permissions, str], requested_post_id: Optional[int],
    ) -> Optional[FrozenSet[int]]:
        """Scope of a list that accepts a post_id filter: the whole scope_for when nothing is requested, only the
        requested post when the permission is in effect there, otherwise 403 (check_post)"""
        if requested_post_id is None:
            return auth.posts_with(permission)
        AccessControlService.check_post(auth, permission, requested_post_id)
        return frozenset({requested_post_id})

    @staticmethod
    def check_post(
            auth: AuthContext, permission: Union[Permissions, str], post_id: int, resource_type: str = "post",
    ) -> None:
        """The permission is in effect at the post, otherwise 403: no_post_assigned when it is in effect at no post
        at all (never held, or held only on archived posts), post_access_denied for a *.view permission,
        post_write_denied for any other"""
        if auth.has_at_post(permission, post_id):
            return
        name = _name(permission)
        required = Require.of(all=[name])
        if auth.posts_with(permission) == frozenset():
            logger.warning(f"User {auth.user.username} holds {name} at no post, tried {resource_type} of post {post_id}")
            raise access_denied(AccessErrorCode.NO_POST_ASSIGNED, required=required, missing=[name])
        logger.warning(f"User {auth.user.username} lacks {name} at post {post_id} ({resource_type})")
        if name.endswith(".view"):
            raise access_denied(AccessErrorCode.POST_ACCESS_DENIED, required=required, missing=[name],
                                resource_type=resource_type, post_id=post_id)
        raise access_denied(AccessErrorCode.POST_WRITE_DENIED, required=required, missing=[name],
                            post_id=post_id, resource_type=resource_type)

    @staticmethod
    def check_covers(auth: AuthContext, permission: Union[Permissions, str], node: Node, action: str) -> None:
        """The node (where a post is created or moved to) lies inside a subtree where the permission is held,
        otherwise 403 permission_required_for_action"""
        if auth.covers(permission, node):
            return
        name = _name(permission)
        logger.warning(f"User {auth.user.username} lacks {name} at {node.key} for {action}")
        raise access_denied(
            AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
            required=Require.of(all=[name]),
            missing=[name],
            permission=name,
            action=action,
        )

    @staticmethod
    def check_user_view(auth: AuthContext, target: User) -> None:
        """A user record is visible to the user itself and to callers with user.view in effect at the user's home
        post (at the root for a user without one), otherwise 403 user_self_only"""
        if target.id == auth.user.id or auth.covers_user(Permissions.USER_VIEW, target):
            return
        logger.warning(f"User {auth.user.username} tried to view user {target.id} outside of scope")
        raise access_denied(AccessErrorCode.USER_SELF_ONLY)

    @staticmethod
    def archived_scope(auth: AuthContext, params, permission: Permissions) -> Optional[FrozenSet[int]]:
        """include_deleted needs the X.archive permission held anywhere (check_include_deleted); archived rows are
        then shown only in the posts where it is in effect. None means no restriction."""
        AccessControlService.check_include_deleted(auth, params, permission)
        return auth.posts_with(permission)

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
        """Explicit permission check inside a handler (for optional request features such as include_deleted):
        the permission is held somewhere, like require()"""
        if auth.has_anywhere(permission):
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
