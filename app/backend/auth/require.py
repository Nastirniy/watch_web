"""Route permission requirements.

Usage on a route:

    auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE]))

Semantics:
- all:   every listed permission is required
- any:   at least one listed permission is required

A permission counts when it is held at some node of the scope tree (AuthContext.held_anywhere): the route is
reachable, the handler then checks where it applies (AccessControlService: check_post, scope_for, ...). There is no
administrator gate: an administration route requires its own permissions, like any other route.
"""
import builtins
from dataclasses import dataclass
from typing import List, Dict, Union, Any, Iterable, Tuple

from fastapi import Depends
from loguru import logger

from app.backend.enums.general import Permissions
from app.backend.enums.errors import AccessErrorCode
from app.backend.auth.dependencies import get_auth_context
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

    @classmethod
    def of(
            cls,
            all: Iterable[Union[Permissions, str]] = (),
            any: Iterable[Union[Permissions, str]] = (),
    ) -> "Require":
        return cls(
            all=tuple(permission_name(p) for p in all),
            any=tuple(permission_name(p) for p in any),
        )

    def evaluate(self, auth: AuthContext) -> PermissionCheckResult:
        held = auth.held_anywhere
        missing: List[str] = [p for p in self.all if p not in held]
        if self.any and not builtins.any(p in held for p in self.any):
            missing.extend(p for p in self.any if p not in missing)
        return PermissionCheckResult(allowed=not missing, missing=tuple(missing))

    def as_dict(self) -> Dict[str, Any]:
        return {"all": list(self.all), "any": list(self.any)}

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
) -> Require:
    """Dependency: Depends(require(all=[...], any=[...])).

    require() with no arguments only requires an authenticated user and still
    registers the route in the route access map.
    """
    return Require.of(all=all, any=any)
