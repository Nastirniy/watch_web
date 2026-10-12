"""Route permission requirements.

Usage on a route:

    auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE], admin=True))

Semantics:
- all:   every listed permission is required
- any:   at least one listed permission is required
- admin: admin.true is required IN ADDITION to the granular permissions. admin.true
         is a gate for the administration area only, it never substitutes a permission
         (transitional: removed once no route sets it).

A permission counts when it is held at some node of the scope tree (AuthContext.held_anywhere): the route is
reachable, the handler then checks where it applies (AccessControlService: check_post, scope_for, ...). A legacy
permission the grants do not carry (dead without admin.true) never counts, so removing the admin gate from a route
never widens it.
"""
import builtins
from dataclasses import dataclass
from typing import List, Dict, Union, Any, Iterable, Tuple

from fastapi import Depends
from loguru import logger

from app.backend.enums.general import Permissions
from app.backend.enums.errors import AccessErrorCode
from app.backend.auth.dependencies import get_auth_context
from app.backend.auth.context import AuthContext
from app.backend.auth.access_errors import access_denied


def _permission_name(permission: Union[Permissions, str]) -> str:
    return permission.value if isinstance(permission, Permissions) else str(permission)


@dataclass(frozen=True)
class PermissionCheckResult:
    allowed: bool
    missing: Tuple[str, ...]
    admin_missing: bool


@dataclass(frozen=True)
class Require:
    all: Tuple[str, ...] = ()
    any: Tuple[str, ...] = ()
    admin: bool = False

    @classmethod
    def of(
            cls,
            all: Iterable[Union[Permissions, str]] = (),
            any: Iterable[Union[Permissions, str]] = (),
            admin: bool = False,
    ) -> "Require":
        return cls(
            all=tuple(_permission_name(p) for p in all),
            any=tuple(_permission_name(p) for p in any),
            admin=bool(admin),
        )

    def evaluate(self, auth: AuthContext) -> PermissionCheckResult:
        held = auth.held_anywhere
        missing: List[str] = [p for p in self.all if p not in held]
        if self.any and not builtins.any(p in held for p in self.any):
            missing.extend(p for p in self.any if p not in missing)
        admin_missing = self.admin and not auth.is_admin
        return PermissionCheckResult(
            allowed=not missing and not admin_missing,
            missing=tuple(missing),
            admin_missing=admin_missing,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {"all": list(self.all), "any": list(self.any), "admin": self.admin}

    async def __call__(self, auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        """A Require is a FastAPI dependency (parameterized dependency): it returns the AuthContext or raises 403"""
        result = self.evaluate(auth)
        if not result.allowed:
            code = AccessErrorCode.ADMIN_REQUIRED if result.admin_missing else AccessErrorCode.PERMISSION_DENIED
            logger.warning(
                f"Access denied for user {auth.user.username}: code={code.value} "
                f"missing={list(result.missing)} admin_required={self.admin}"
            )
            raise access_denied(code, required=self, missing=result.missing)
        return auth


def require(
        *,
        all: Iterable[Union[Permissions, str]] = (),
        any: Iterable[Union[Permissions, str]] = (),
        admin: bool = False,
) -> Require:
    """Dependency: Depends(require(all=[...], any=[...], admin=True)).

    require() with no arguments only requires an authenticated user and still
    registers the route in the route access map.
    """
    return Require.of(all=all, any=any, admin=admin)
