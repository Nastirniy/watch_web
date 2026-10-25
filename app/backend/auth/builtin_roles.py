"""The built-in roles: owned by code, seeded and re-synced at every start (bootstrap/permissions.py), read-only in the
API. Their names never carry a place: administrator at a post is the administrator of the post, at a brigade of the
brigade, at the root of all data. Tree roles hold only tree permissions, system roles only system permissions.

Nothing here reaches the database or FastAPI.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet

from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.enums.general import BuiltinRole, PermissionLevel, Permissions, RoleKind


def _names(*members: Permissions) -> FrozenSet[str]:
    return frozenset(member.value for member in members)


P = Permissions

VIEW = _names(P.POST_VIEW, P.OBSERVATION_VIEW, P.MEDIA_VIEW, P.CATEGORY_VIEW, P.SIGNAL_TYPE_VIEW, P.TAG_VIEW)
AGGREGATES = _names(P.STATISTICS_VIEW, P.EXPORT_CREATE)
OPERATOR = VIEW | _names(
    P.OBSERVATION_CREATE, P.OBSERVATION_UPDATE, P.OBSERVATION_ARCHIVE, P.MEDIA_CREATE, P.MEDIA_ARCHIVE,
    P.MEDIA_FORMAT_VIEW,
)
OVERRIDES = _names(P.OBSERVATION_MANAGE_ANY, P.MEDIA_MANAGE_ANY)
SENIOR_OPERATOR = OPERATOR | OVERRIDES | AGGREGATES
ANALYST = VIEW | AGGREGATES
EVERY_TREE = frozenset(name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.TREE)
EVERY_SYSTEM = frozenset(name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.SYSTEM)
TAG_EDIT = _names(P.TAG_CREATE, P.TAG_UPDATE)
# Create and update only: archiving dictionaries stays with system_administrator
DICTIONARY_EDIT = TAG_EDIT | _names(P.CATEGORY_CREATE, P.CATEGORY_UPDATE, P.SIGNAL_TYPE_CREATE, P.SIGNAL_TYPE_UPDATE)


@dataclass(frozen=True)
class BuiltinRoleSpec:
    key: BuiltinRole
    kind: RoleKind
    display_name: str
    description: str
    permissions: FrozenSet[str]


# The order is the order of the catalog
BUILTIN_ROLES: Dict[BuiltinRole, BuiltinRoleSpec] = {spec.key: spec for spec in (
    BuiltinRoleSpec(BuiltinRole.VIEWER, RoleKind.TREE, "Перегляд",
                    "Перегляд постів, спостережень і медіафайлів", VIEW),
    BuiltinRoleSpec(BuiltinRole.OPERATOR, RoleKind.TREE, "Оператор посту",
                    "Веде спостереження та медіафайли", OPERATOR),
    BuiltinRoleSpec(BuiltinRole.SENIOR_OPERATOR, RoleKind.TREE, "Старший оператор",
                    "Керує чужими спостереженнями та медіафайлами, бачить статистику й експорти", SENIOR_OPERATOR),
    BuiltinRoleSpec(BuiltinRole.ANALYST, RoleKind.TREE, "Аналітик",
                    "Перегляд даних, статистика та експорти", ANALYST),
    BuiltinRoleSpec(BuiltinRole.ADMINISTRATOR, RoleKind.TREE, "Адміністратор",
                    "Усі права на вузлі призначення: користувачі, пости, підрозділи, дані, сертифікати", EVERY_TREE),
    BuiltinRoleSpec(BuiltinRole.SYSTEM_ADMINISTRATOR, RoleKind.SYSTEM, "Системний адміністратор",
                    "Ролі, довідники, формати, аудит, моніторинг, бекапи, експорт сертифікатів", EVERY_SYSTEM),
    BuiltinRoleSpec(BuiltinRole.AUDITOR, RoleKind.SYSTEM, "Аудитор",
                    "Перегляд журналу аудиту", _names(P.AUDIT_VIEW)),
    BuiltinRoleSpec(BuiltinRole.DICTIONARY_EDITOR, RoleKind.SYSTEM, "Редактор довідників",
                    "Створює та змінює категорії, типи сигналів і мітки", DICTIONARY_EDIT),
    BuiltinRoleSpec(BuiltinRole.TAG_EDITOR, RoleKind.SYSTEM, "Редактор міток",
                    "Створює та змінює мітки", TAG_EDIT),
)}
