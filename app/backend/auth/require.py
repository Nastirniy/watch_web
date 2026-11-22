"""Route permission requirements.

Usage on a route:

    auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE]))

Semantics:
- all:     every listed permission is required
- any:     at least one listed permission is required
- at_root: every listed permission is required in effect at the root

A permission of all / any counts when it is held at some node of the scope tree that is not archived
(AuthContext.held_anywhere): the route is reachable, the handler then checks where it applies (AccessControlService:
check_post, scope_for, ...). A permission of at_root counts when it is held at the root (a tree grant there, or a
system role): for a route that acts on the whole tree (every post, a grant at the root), where a holder further down
could only ever get a 403 from the handler. GET /auth/route-access follows from the same evaluate.
There is no administrator gate: an administration route requires its own permissions, like any other route.
"""
import builtins
from dataclasses import dataclass
from typing import List, Dict, Optional, Union, Any, Iterable, Tuple

from fastapi import Depends
from loguru import logger

from app.backend.enums.general import Permissions
from app.backend.enums.errors import AccessErrorCode
from app.backend.auth.dependencies import get_auth_context, get_stream_auth_context
from app.backend.auth.context import AuthContext, permission_name
from app.backend.auth.access_errors import access_denied


@dataclass(frozen=True)
class PermissionCheckResult:
    allowed: bool
    missing: Tuple[str, ...]


@dataclass(frozen=True)
class Require:
    all: Tuple[str, ...] = ()
    any: Tuple[str, ...] = ()
    at_root: Tuple[str, ...] = ()

    @classmethod
    def of(
            cls,
            all: Iterable[Union[Permissions, str]] = (),
            any: Iterable[Union[Permissions, str]] = (),
            at_root: Iterable[Union[Permissions, str]] = (),
    ) -> "Require":
        return cls(
            all=tuple(permission_name(p) for p in all),
            any=tuple(permission_name(p) for p in any),
            at_root=tuple(permission_name(p) for p in at_root),
        )

    def evaluate(self, auth: AuthContext, post_id: Optional[int] = None) -> PermissionCheckResult:
        """all and any held anywhere (the route requirement); with post_id: in effect at that post (POST
        /auth/check-permission). at_root held at the root in both cases."""
        held = auth.held_anywhere if post_id is None else auth.permissions_at_post(post_id)
        missing: List[str] = [p for p in self.all if p not in held]
        if self.any and not builtins.any(p in held for p in self.any):
            missing.extend(p for p in self.any if p not in missing)
        missing.extend(p for p in self.at_root if not auth.has_at_root(p) and p not in missing)
        return PermissionCheckResult(allowed=not missing, missing=tuple(missing))

    def as_dict(self) -> Dict[str, Any]:
        return {"all": list(self.all), "any": list(self.any), "at_root": list(self.at_root)}

    async def __call__(self, auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        """A Require is a FastAPI dependency (parameterized dependency): it returns the AuthContext or raises 403"""
        result = self.evaluate(auth)
        if not result.allowed:
            logger.warning(f"Access denied for user {auth.user.username}: missing={list(result.missing)}")
            raise access_denied(AccessErrorCode.PERMISSION_DENIED, required=self, missing=result.missing)
        return auth


def require(
        *,
        all: Iterable[Union[Permissions, str]] = (),
        any: Iterable[Union[Permissions, str]] = (),
        at_root: Iterable[Union[Permissions, str]] = (),
) -> Require:
    """Dependency: Depends(require(all=[...], any=[...], at_root=[...])).

    require() with no arguments only requires an authenticated user and still
    registers the route in the route access map.
    """
    return Require.of(all=all, any=any, at_root=at_root)


@dataclass(frozen=True)
class StreamRequire(Require):
    """require() for the stream of events: the same checks, no touch. A stream is not activity. Still a Require, so
    route_access_map and GET /auth/route-access find the route."""

    async def __call__(self, auth: AuthContext = Depends(get_stream_auth_context)) -> AuthContext:
        return await super().__call__(auth)


def require_stream() -> StreamRequire:
    """Dependency of the stream route: any authenticated user, Depends(require_stream())"""
    return StreamRequire()
