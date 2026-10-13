from typing import Optional, FrozenSet, Union
from loguru import logger

from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.errors import AccessErrorCode
from app.backend.models.user import User
from app.backend.models.observation import Observation
from app.backend.models.media import MediaFile
from app.backend.auth.context import AuthContext, permission_name
from app.backend.auth.scope import Node
from app.backend.auth.access_errors import access_denied
from app.backend.auth.require import Require


class AccessControlService:
    """Data scope and ownership checks. All checks read the per-request AuthContext and never query the database.

    Permission at the post (authorization v2, spec section 5): require() on the route checks that the permission is
    held somewhere; the handler checks where it applies with the permission itself:
    - scope_for / resolve_scope: the posts a list covers (posts_with: None = every post)
    - check_post: one post (403 post_access_denied for *.view, post_write_denied otherwise). A change to the data of
      a post checks X.view there first, then the permission of the change, so a caller who cannot see the post
      learns nothing more than post_access_denied
    - check_covers: where a post is created or moved to (403 permission_required_for_action)
    - check_user_view: one user record (the caller, or user.view covering the user's home post)
    - check_user_permission: what belongs to a user beyond the record (certificates): the permission covering the
      user's home post, for the user themselves too
    - archived_scope: include_deleted needs X.archive held anywhere; archived rows only where it is in effect
    - ownership: the owner, or X.manage_any in effect at the post of the row
    Grants on an archived post stop applying, grants inherited from a group or the root keep applying; writes to the
    data of an archived post answer 409 post_archived (api/shared/helpers/post_helper.py). There is no administrator
    exception: post records follow post.view, user records user.view, data the data permissions.
    """

    # ------------------------------------------------------- permission at the post
    @staticmethod
    def scope_for(auth: AuthContext, permission: Union[Permissions, str]) -> Optional[FrozenSet[int]]:
        """Post ids where the permission is in effect, to filter a list by. None means every post."""
        return auth.posts_with(permission)

    @staticmethod
    def scope_for_all(auth: AuthContext, *permissions: Union[Permissions, str]) -> Optional[FrozenSet[int]]:
        """Post ids where every one of the permissions is in effect (an export: the data read and the export right).
        None means every post."""
        scope: Optional[FrozenSet[int]] = None
        for permission in permissions:
            posts = auth.posts_with(permission)
            if posts is not None:
                scope = posts if scope is None else scope & posts
        return scope

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
        at all (never held, or held only at groups without posts; a grant only on archived nodes is held nowhere and
        require() answers first), post_access_denied for a *.view permission, post_write_denied for any other"""
        if auth.has_at_post(permission, post_id):
            return
        name = permission_name(permission)
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
        name = permission_name(permission)
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
        """A user record (and what belongs to it: sessions, assignments) is visible to the user itself and to
        callers with user.view in effect at the user's home post (at the root for a user without one), otherwise 403
        user_self_only"""
        if target.id == auth.user.id or auth.covers_user(Permissions.USER_VIEW, target):
            return
        logger.warning(f"User {auth.user.username} tried to view user {target.id} outside of scope")
        raise access_denied(AccessErrorCode.USER_SELF_ONLY)

    @staticmethod
    def check_user_permission(auth: AuthContext, permission: Union[Permissions, str], target: User) -> None:
        """The permission is in effect at the user's home post (at the root for a user without one), otherwise 403
        permission_denied: what belongs to a user record beyond the record itself (the user's certificates follow
        certificate.view), the user themselves included"""
        if auth.covers_user(permission, target):
            return
        name = permission_name(permission)
        logger.warning(f"User {auth.user.username} lacks {name} for user {target.id}")
        raise access_denied(AccessErrorCode.PERMISSION_DENIED, required=Require.of(all=[name]), missing=[name])

    @staticmethod
    def archived_scope(auth: AuthContext, params, permission: Permissions) -> Optional[FrozenSet[int]]:
        """include_deleted needs the X.archive permission held anywhere (check_include_deleted); archived rows are
        then shown only in the posts where it is in effect. None means no restriction; without include_deleted no
        archived row is shown anywhere (an empty set)."""
        if not getattr(params, "include_deleted", False):
            return frozenset()
        AccessControlService.check_include_deleted(auth, params, permission)
        return auth.posts_with(permission)

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
        """Owner, or observation.manage_any in effect at the observation's post"""
        if observation.user_id == auth.user.id or auth.has_at_post(Permissions.OBSERVATION_MANAGE_ANY, observation.post_id):
            return
        logger.warning(f"User {auth.user.username} is not the owner of observation {observation.id}")
        raise access_denied(
            AccessErrorCode.OBSERVATION_NOT_OWNER,
            required=Require.of(all=[Permissions.OBSERVATION_MANAGE_ANY]),
            missing=[Permissions.OBSERVATION_MANAGE_ANY.value],
        )

    @staticmethod
    def check_media_ownership(auth: AuthContext, media_file: MediaFile, observation: Optional[Observation]) -> None:
        """media.manage_any in effect at the post of the media's observation (at the root for media without an
        observation) bypasses everything. Otherwise the observation must be active (when attached) and the caller
        must be the uploader."""
        if observation is None:
            manages = auth.has_at_root(Permissions.MEDIA_MANAGE_ANY)
        else:
            manages = auth.has_at_post(Permissions.MEDIA_MANAGE_ANY, observation.post_id)
        if manages:
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
