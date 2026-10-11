"""The built-in admin role and the display texts of every permission; seeding keeps the database in sync."""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.utils.logging_config import get_database_logger

_logger = get_database_logger()

ADMIN_ROLE_NAME = "admin"  # built-in role: seeded, cannot be archived or deleted

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
}


async def seed_roles_and_permissions(session: AsyncSession):
    """Idempotent: ensure the admin role, every Permission row from the enum
    (display texts refreshed from PERMISSION_META), and that the admin role holds
    every permission (re-granted on every run). Renamed permissions are handled by
    migrations (010_archive_permissions), never here.

    Returns the admin Role.
    """
    from app.backend.models.role import Role, Permission, RolePermission
    from app.backend.enums.general import Permissions

    result = await session.exec(select(Role).where(Role.name == ADMIN_ROLE_NAME))
    admin_role = result.first()
    if not admin_role:
        admin_role = Role(
            name=ADMIN_ROLE_NAME,
            display_name="Administrator",
            description="Full system access"
        )
        session.add(admin_role)
        await session.flush()
        _logger.info("Admin role created")
    elif admin_role.is_deleted:
        admin_role.is_deleted = False
        session.add(admin_role)
        await session.flush()
        _logger.warning("Admin role was soft-deleted; restored by seeding")

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

    # Re-grant: the admin role must hold every permission after every startup
    result = await session.exec(select(Permission.id))
    all_permission_ids = set(result.all())
    result = await session.exec(
        select(RolePermission.permission_id).where(RolePermission.role_id == admin_role.id)
    )
    granted_ids = set(result.all())
    missing_ids = sorted(all_permission_ids - granted_ids)
    for permission_id in missing_ids:
        session.add(RolePermission(role_id=admin_role.id, permission_id=permission_id))
    if missing_ids:
        _logger.info(f"Re-granted {len(missing_ids)} permissions to the admin role")

    await session.commit()
    return admin_role
