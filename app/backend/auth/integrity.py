"""The rules of the authorization model checked in one place (authorization v3).

A grant puts in effect only the permissions of its role that have the level of the role's kind and that the
boundary of the user's type allows for that kind (auth/user_types.py: the tree boundary for tree grants, the system
boundary for system grants). This holds whatever wrote the rows: the API refuses such writes with its 409s, and
evaluation (auth/context.py) keeps what a bug, a migration or seeding could still write from ever acting.

Nothing here reaches the database or FastAPI.
"""
from typing import FrozenSet, Iterable, List, Tuple, Union

from app.backend.auth.user_types import boundary
from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.enums.general import RoleKind, UserType


def other_level(kind: Union[RoleKind, str], names: Iterable[str]) -> List[str]:
    """The names a role of the kind cannot hold (another level, or unknown to the catalog), sorted"""
    kind = RoleKind(kind)
    return sorted(name for name in set(names)
                  if PERMISSION_LEVELS.get(name) is None or PERMISSION_LEVELS[name].value != kind.value)


def effective_permissions(user_type: Union[UserType, str], kind: Union[RoleKind, str],
                          names: Iterable[str]) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """(kept, left out) of the permissions a grant of the kind holds for a user of the type. A name the catalog does
    not know (a row left in the database) is left out."""
    kind = RoleKind(kind)
    allowed = boundary(UserType(user_type), kind)
    names = frozenset(names)
    kept = frozenset(name for name in names
                     if name in allowed and PERMISSION_LEVELS.get(name) is not None
                     and PERMISSION_LEVELS[name].value == kind.value)
    return kept, names - kept
