"""The user types: the catalog of positions, with the base roles and the boundary of every type.

Every user has exactly one type. A new user gets the base roles of the type: a tree role at the home node (the home
post, the root without one, or always the root for at_root) and the system roles. The boundary is the most a user of
the type can ever have in effect, whatever the roles say: the tree boundary for tree grants, the system boundary for
system grants (AWS permissions boundary style: auth/integrity.py applies it at every request; the API also refuses an
assignment outside it). The base roles always fit the boundary.

Nothing here reaches the database or FastAPI: authorization reads this module, seeding reads it, the API publishes it.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

from app.backend.auth.builtin_roles import (
    AGGREGATES, ANALYST, BUILTIN_ROLES, DICTIONARY_EDIT, EVERY_SYSTEM, EVERY_TREE, OPERATOR, OVERRIDES,
    SENIOR_OPERATOR, TAG_EDIT, VIEW, _names,
)
from app.backend.enums.general import BuiltinRole, Permissions, RoleKind, UserType

P = Permissions


@dataclass(frozen=True)
class BaseRole:
    role: BuiltinRole
    at_root: bool = False


@dataclass(frozen=True)
class UserTypeSpec:
    key: UserType
    display_name: str
    description: str
    base_roles: Tuple[BaseRole, ...]
    tree_boundary: FrozenSet[str]
    system_boundary: FrozenSet[str]

    @property
    def tree_base(self) -> BaseRole:
        """The tree base role (every type has exactly one)"""
        return next(base for base in self.base_roles if BUILTIN_ROLES[base.role].kind is RoleKind.TREE)

    @property
    def system_bases(self) -> Tuple[BaseRole, ...]:
        return tuple(base for base in self.base_roles if BUILTIN_ROLES[base.role].kind is RoleKind.SYSTEM)

    @property
    def base_permissions(self) -> FrozenSet[str]:
        """Every permission of the base roles"""
        return frozenset().union(*(BUILTIN_ROLES[base.role].permissions for base in self.base_roles))

    @property
    def allowed_permissions(self) -> FrozenSet[str]:
        """Both boundaries"""
        return self.tree_boundary | self.system_boundary


def _spec(key: UserType, display_name: str, description: str, base_roles: Tuple[BaseRole, ...],
          tree: FrozenSet[str], system: FrozenSet[str]) -> UserTypeSpec:
    return UserTypeSpec(key, display_name, description, base_roles, tree, system)


# The order is the order of the catalog
USER_TYPES: Dict[UserType, UserTypeSpec] = {spec.key: spec for spec in (
    _spec(
        UserType.VIEWER, "Перегляд", "Лише перегляд постів, спостережень і медіафайлів",
        (BaseRole(BuiltinRole.VIEWER),), VIEW | AGGREGATES, frozenset(),
    ),
    _spec(
        UserType.OPERATOR, "Оператор посту", "Веде спостереження та медіафайли свого посту",
        (BaseRole(BuiltinRole.OPERATOR),), OPERATOR | OVERRIDES | AGGREGATES, TAG_EDIT,
    ),
    _spec(
        UserType.SENIOR_OPERATOR, "Старший оператор",
        "Оператор, який керує чужими спостереженнями та медіафайлами, бачить статистику й експорти",
        (BaseRole(BuiltinRole.SENIOR_OPERATOR),), SENIOR_OPERATOR | _names(P.MEDIA_UPDATE, P.USER_VIEW), TAG_EDIT,
    ),
    _spec(
        UserType.ANALYST, "Аналітик", "Перегляд даних, статистика та експорти",
        (BaseRole(BuiltinRole.ANALYST),), ANALYST | _names(P.USER_VIEW, P.SCOPE_GROUP_VIEW), frozenset(),
    ),
    _spec(
        UserType.ADMINISTRATOR, "Адміністратор",
        "Керує користувачами, постами, підрозділами й даними свого вузла: поста, бригади, корпусу або всіх даних",
        (BaseRole(BuiltinRole.ADMINISTRATOR),), EVERY_TREE, DICTIONARY_EDIT,
    ),
    _spec(
        UserType.SYSTEM_ADMINISTRATOR, "Системний адміністратор", "Повний доступ до системи",
        (BaseRole(BuiltinRole.ADMINISTRATOR, at_root=True), BaseRole(BuiltinRole.SYSTEM_ADMINISTRATOR, at_root=True)),
        EVERY_TREE, EVERY_SYSTEM,
    ),
)}


def boundary(user_type: UserType, kind: RoleKind) -> FrozenSet[str]:
    """The boundary of the type for grants of the kind"""
    spec = USER_TYPES[UserType(user_type)]
    return spec.tree_boundary if kind is RoleKind.TREE else spec.system_boundary


def excess_permissions(user_type: UserType, permission_names: Iterable[str],
                       kind: Optional[RoleKind] = None) -> FrozenSet[str]:
    """The permissions among permission_names outside the boundary of the type for the kind (None: both)"""
    allowed = USER_TYPES[UserType(user_type)].allowed_permissions if kind is None else boundary(user_type, kind)
    return frozenset(permission_names) - allowed
