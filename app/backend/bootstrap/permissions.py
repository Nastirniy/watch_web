"""The display texts and levels of every permission and the built-in roles; seeding keeps the database in sync."""
from typing import Dict, Iterable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.enums.general import PermissionLevel, Permissions, UserType
from app.backend.utils.logging_config import get_database_logger

_logger = get_database_logger()

PERMISSION_META = {
    "user.view": ("Перегляд користувачів", "Дозволяє переглядати інформацію та профілі користувачів"),
    "user.create": ("Створення користувачів", "Дозволяє створювати нові облікові записи користувачів"),
    "user.update": ("Оновлення користувачів", "Дозволяє змінювати інформацію та налаштування користувачів"),
    "user.archive": ("Архівування користувачів", "Дозволяє архівувати та відновлювати облікові записи користувачів. Архівований користувач втрачає доступ і зникає зі списків, але його дані та спостереження зберігаються; також дозволяє бачити архівованих у списках (include_deleted)"),
    "post.view": ("Перегляд посту", "Дозволяє переглядати пости та їх деталі"),
    "post.create": ("Створення посту", "Дозволяє створювати нові пости"),
    "post.update": ("Оновлення посту", "Дозволяє редагувати існуючі пости"),
    "post.archive": ("Архівування постів", "Дозволяє архівувати та відновлювати пости. Дані не видаляються"),
    "observation.view": ("Перегляд спостережень", "Дозволяє переглядати сигнальні спостереження та їх дані"),
    "observation.create": ("Створення спостережень", "Дозволяє створювати нові сигнальні спостереження"),
    "observation.update": ("Оновлення спостережень", "Дозволяє змінювати існуючі сигнальні спостереження"),
    "observation.archive": ("Архівування спостережень", "Дозволяє архівувати та відновлювати власні спостереження (чужі - разом з observation.manage_any). Архівоване спостереження приховується зі списків, медіафайли залишаються прикріпленими; також дозволяє бачити архівовані у списках (include_deleted)"),
    "role.view": ("Перегляд ролей", "Дозволяє переглядати інформацію про ролі та дозволи"),
    "role.create": ("Створення ролей", "Дозволяє створювати нові ролі та призначати дозволи"),
    "role.update": ("Оновлення ролей", "Дозволяє змінювати існуючі ролі та їх дозволи"),
    "role.archive": ("Архівування ролей", "Дозволяє архівувати та відновлювати ролі. Архівована роль одразу перестає надавати дозволи, її не можна призначати, але призначення користувачам зберігаються; також дозволяє бачити архівовані ролі у списках (include_deleted)"),
    "role.delete": ("Остаточне видалення ролей", "НЕБЕЗПЕЧНО: безповоротно видаляє архівовану роль без призначених користувачів разом з усіма її дозволами. Це єдина операція в системі, яка фізично видаляє дані; відновлення неможливе"),
    "admin.true": ("Адміністратор", "Повний доступ до системи з усіма адміністративними привілеями"),
    "audit.view": ("Перегляд аудиту", "Дозволяє переглядати системні журнали аудиту та історію активності"),
    "export.create": ("Створення експортів", "Дозволяє створювати та завантажувати експорти даних"),
    "category.view": ("Перегляд категорій", "Дозволяє переглядати інформацію про категорії"),
    "category.create": ("Створення категорій", "Дозволяє створювати нові категорії"),
    "category.update": ("Оновлення категорій", "Дозволяє змінювати існуючі категорії"),
    "category.archive": ("Архівування категорій", "Дозволяє архівувати та відновлювати категорії. Архівована категорія приховується зі списків, дані зберігаються; також дозволяє бачити архівовані у списках (include_deleted)"),
    "signal_type.view": ("Перегляд типів сигналів", "Дозволяє переглядати інформацію про типи сигналів"),
    "signal_type.create": ("Створення типів сигналів", "Дозволяє створювати нові типи сигналів"),
    "signal_type.update": ("Оновлення типів сигналів", "Дозволяє змінювати існуючі типи сигналів"),
    "signal_type.archive": ("Архівування типів сигналів", "Дозволяє архівувати та відновлювати типи сигналів. Тип з активними спостереженнями архівувати не можна; також дозволяє бачити архівовані у списках (include_deleted)"),
    "tag.view": ("Перегляд тегів", "Дозволяє переглядати інформацію про теги"),
    "tag.create": ("Створення тегів", "Дозволяє створювати нові теги"),
    "tag.update": ("Оновлення тегів", "Дозволяє змінювати існуючі теги"),
    "tag.archive": ("Архівування тегів", "Дозволяє архівувати та відновлювати теги. Архівований тег залишається прикріпленим до спостережень, але його не можна додавати до нових; також дозволяє бачити архівовані у списках (include_deleted)"),
    "scope_group.view": ("Перегляд груп постів", "Дозволяє переглядати групи постів і дерево груп"),
    "scope_group.create": ("Створення груп постів", "Дозволяє створювати нові групи постів"),
    "scope_group.update": ("Оновлення груп постів", "Дозволяє змінювати групи постів і переносити їх в інші групи"),
    "scope_group.archive": ("Архівування груп постів", "Дозволяє архівувати та відновлювати групи постів. Архівувати можна лише порожню групу; також дозволяє бачити архівовані у списках (include_deleted)"),
    "media.view": ("Перегляд медіафайлів", "Дозволяє переглядати прикріплені медіафайли"),
    "media.create": ("Завантаження медіафайлів", "Дозволяє завантажувати нові медіафайли"),
    "media.update": ("Оновлення медіафайлів", "Дозволяє змінювати інформацію про медіафайли"),
    "media.archive": ("Архівування медіафайлів", "Дозволяє архівувати та відновлювати власні медіафайли (чужі - разом з media.manage_any). Архівований файл приховується зі спостереження, але залишається на диску"),
    "post.access_all": ("Доступ до всіх постів", "Дозволяє переглядати та працювати з даними всіх постів, а не лише призначених"),
    "observation.manage_any": ("Керування чужими спостереженнями", "Дозволяє змінювати, закривати, архівувати та відновлювати спостереження, створені іншими користувачами"),
    "media.manage_any": ("Керування чужими медіафайлами", "Дозволяє архівувати та відновлювати медіафайли, завантажені іншими користувачами"),
    "media_format.view": ("Перегляд форматів медіа", "Дозволяє переглядати перелік прийнятних форматів завантаження"),
    "media_format.create": ("Створення форматів медіа", "Дозволяє додавати прийнятні формати завантаження"),
    "media_format.update": ("Оновлення форматів медіа", "Дозволяє змінювати прийнятні формати та обмеження розміру файлів"),
    "media_format.archive": ("Архівування форматів медіа", "Дозволяє архівувати та відновлювати формати завантаження. Архівований формат одразу перестає прийматися, завантажені файли не змінюються; також дозволяє бачити архівовані у списках (include_deleted)"),
    "statistics.view": ("Перегляд статистики", "Дозволяє переглядати зведену статистику: адміністратор бачить усі пости, інші користувачі - лише доступні їм пости"),
    "media.monitor": ("Моніторинг медіасховища", "Дозволяє переглядати стан сховища медіафайлів і черги обробки мініатюр"),
}

# Permissions assignable only at the root of the scope tree; every other permission is scoped
_SYSTEM_PERMISSIONS = frozenset({
    "role.create", "role.update", "role.archive", "role.delete",
    "category.create", "category.update", "category.archive",
    "signal_type.create", "signal_type.update", "signal_type.archive",
    "tag.create", "tag.update", "tag.archive",
    "media_format.create", "media_format.update", "media_format.archive",
    "scope_group.create", "scope_group.update", "scope_group.archive",
    "media.monitor", "audit.view", "admin.true",
})

PERMISSION_LEVELS: Dict[str, PermissionLevel] = {
    perm.value: PermissionLevel.SYSTEM if perm.value in _SYSTEM_PERMISSIONS else PermissionLevel.SCOPED
    for perm in Permissions
}


def permission_level(name: str) -> PermissionLevel:
    return PERMISSION_LEVELS[name]


def role_level(permission_names: Iterable[str]) -> PermissionLevel:
    """A role is system when it holds at least one system permission: it can be assigned only at the root"""
    if any(permission_level(name) is PermissionLevel.SYSTEM for name in permission_names):
        return PermissionLevel.SYSTEM
    return PermissionLevel.SCOPED


async def sync_builtin_roles(session: AsyncSession) -> Dict[UserType, "Role"]:
    """One built-in role per user type, owned by code: found by builtin_key, else a role already named like the
    key is claimed, else created. Display texts, the archive flag and the permission set (the base of the type)
    are restored at every start. The permissions must already be seeded. Does not commit."""
    from app.backend.auth.user_types import USER_TYPES
    from app.backend.models.role import Role, Permission, RolePermission

    permission_ids = {name: pid for pid, name in (await session.exec(select(Permission.id, Permission.name))).all()}
    roles: Dict[UserType, Role] = {}
    for user_type, spec in USER_TYPES.items():
        key = user_type.value
        role = (await session.exec(select(Role).where(Role.builtin_key == key))).first()
        if role is None:
            role = (await session.exec(select(Role).where(Role.name == key))).first()
            if role is not None:
                _logger.warning(f"Role '{key}' claimed as the built-in role of the user type")
        if role is None:
            role = Role(name=key, display_name=spec.display_name, description=spec.description, builtin_key=key)
            session.add(role)
            await session.flush()
            _logger.info(f"Built-in role '{key}' created")
        else:
            if role.is_deleted:
                _logger.warning(f"Built-in role '{key}' was archived; restored by seeding")
            role.builtin_key = key
            role.name = key
            role.display_name = spec.display_name
            role.description = spec.description
            role.is_deleted = False
            session.add(role)
            await session.flush()

        wanted = {permission_ids[name] for name in spec.base_permissions}
        present = set()
        for grant in (await session.exec(select(RolePermission).where(RolePermission.role_id == role.id))).all():
            if grant.permission_id in wanted:
                present.add(grant.permission_id)
            else:
                await session.delete(grant)
        for permission_id in sorted(wanted - present):
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        await session.flush()
        roles[user_type] = role
    return roles


async def seed_roles_and_permissions(session: AsyncSession):
    """Idempotent: ensure every Permission row from the enum (display texts refreshed from PERMISSION_META)
    and the built-in role of every user type with exactly its base permissions (sync_builtin_roles; the admin
    role holds every permission, re-granted on every run). Renamed permissions are handled by migrations
    (010_archive_permissions), never here.

    Returns the admin Role.
    """
    from app.backend.models.role import Permission

    result = await session.exec(select(Permission))
    existing_permissions = {p.name: p for p in result.all()}

    permissions_added = 0
    permissions_refreshed = 0
    for perm in Permissions:
        display_name, description = PERMISSION_META.get(perm.value, (perm.value, ""))
        row = existing_permissions.get(perm.value)
        if row is None:
            session.add(Permission(name=perm.value, display_name=display_name, description=description))
            permissions_added += 1
        elif row.display_name != display_name or row.description != description:
            # Display texts are code-owned (there is no API to edit them), keep the database in sync
            row.display_name = display_name
            row.description = description
            session.add(row)
            permissions_refreshed += 1
    if permissions_added or permissions_refreshed:
        await session.flush()
        _logger.info(f"Seeded {permissions_added} new permissions, refreshed {permissions_refreshed} descriptions")

    roles = await sync_builtin_roles(session)
    await session.commit()
    return roles[UserType.ADMIN]
