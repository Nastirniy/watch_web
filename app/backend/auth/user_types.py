"""The user types: the catalog of what a user can be, with the base role and the ceiling of every type.

Every user has exactly one type. The base permissions are the permissions of the built-in role with the same key
(seeded and re-synced at every start by bootstrap/permissions.py); the allowed permissions are the ceiling: a role
assigned to a user of this type may hold nothing outside it. The ceiling always contains the base.

Nothing here reaches the database or FastAPI: authorization reads this module, seeding reads it, the API publishes it.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable

from app.backend.enums.general import Permissions, UserType


def _names(*members: Permissions) -> FrozenSet[str]:
    return frozenset(member.value for member in members)


P = Permissions

VIEW = _names(P.POST_VIEW, P.OBSERVATION_VIEW, P.MEDIA_VIEW, P.CATEGORY_VIEW, P.SIGNAL_TYPE_VIEW, P.TAG_VIEW)
AGGREGATES = _names(P.STATISTICS_VIEW, P.EXPORT_CREATE)
OPERATOR_BASE = VIEW | _names(
    P.OBSERVATION_CREATE, P.OBSERVATION_UPDATE, P.OBSERVATION_ARCHIVE, P.MEDIA_CREATE, P.MEDIA_ARCHIVE,
    P.MEDIA_FORMAT_VIEW,
)
OVERRIDES = _names(P.OBSERVATION_MANAGE_ANY, P.MEDIA_MANAGE_ANY)
TAG_EDIT = _names(P.TAG_CREATE, P.TAG_UPDATE)
SENIOR_BASE = OPERATOR_BASE | OVERRIDES | AGGREGATES
POST_ADMIN_BASE = SENIOR_BASE | _names(
    P.MEDIA_UPDATE, P.POST_CREATE, P.POST_UPDATE, P.POST_ARCHIVE,
    P.USER_VIEW, P.USER_CREATE, P.USER_UPDATE, P.USER_ARCHIVE, P.ROLE_VIEW, P.SCOPE_GROUP_VIEW,
    P.CERTIFICATE_VIEW, P.CERTIFICATE_CREATE, P.CERTIFICATE_REVOKE,
)
EVERY_PERMISSION = frozenset(member.value for member in Permissions)


@dataclass(frozen=True)
class UserTypeSpec:
    key: UserType
    display_name: str
    description: str
    base_permissions: FrozenSet[str]
    allowed_permissions: FrozenSet[str]


def _spec(key: UserType, display_name: str, description: str, base: FrozenSet[str], extra: FrozenSet[str]) -> UserTypeSpec:
    return UserTypeSpec(key, display_name, description, base, base | extra)


# The order is the order of the catalog
USER_TYPES: Dict[UserType, UserTypeSpec] = {spec.key: spec for spec in (
    _spec(
        UserType.VIEWER, "Перегляд",
        "Лише перегляд постів, спостережень і медіафайлів", VIEW, AGGREGATES,
    ),
    _spec(
        UserType.OPERATOR, "Оператор посту",
        "Веде спостереження та медіафайли свого посту", OPERATOR_BASE,
        OVERRIDES | AGGREGATES | TAG_EDIT,
    ),
    _spec(
        UserType.SENIOR_OPERATOR, "Старший оператор",
        "Оператор, який керує чужими спостереженнями та медіафайлами, бачить статистику й експорти", SENIOR_BASE,
        _names(P.MEDIA_UPDATE, P.USER_VIEW) | TAG_EDIT,
    ),
    _spec(
        UserType.ANALYST, "Аналітик",
        "Перегляд даних, статистика та експорти", VIEW | AGGREGATES,
        _names(P.USER_VIEW, P.SCOPE_GROUP_VIEW),
    ),
    _spec(
        UserType.POST_ADMIN, "Адміністратор посту",
        "Керує користувачами та записами посту й даними старшого оператора", POST_ADMIN_BASE,
        _names(
            P.TAG_CREATE, P.TAG_UPDATE, P.CATEGORY_CREATE, P.CATEGORY_UPDATE, P.SIGNAL_TYPE_CREATE, P.SIGNAL_TYPE_UPDATE,
        ),
    ),
    _spec(
        UserType.ADMIN, "Адміністратор",
        "Повний доступ до системи", EVERY_PERMISSION, frozenset(),
    ),
)}


def excess_permissions(user_type: UserType, permission_names: Iterable[str]) -> FrozenSet[str]:
    """The permissions among permission_names that the ceiling of the type does not allow"""
    return frozenset(permission_names) - USER_TYPES[user_type].allowed_permissions
