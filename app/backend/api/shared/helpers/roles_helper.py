from typing import Any, Dict, List, Optional, Set
import sqlalchemy as sa
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.backend.models.role import Role, Permission, RolePermission
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import ErrorCode


async def get_role_or_404(session: AsyncSession, role_id: int, include_deleted: bool = False,
                          lock: bool = False) -> Role:
    """Get role by ID or raise 404. Soft-deleted roles are 404 unless include_deleted is set.
    lock=True: SELECT ... FOR UPDATE (nothing on sqlite), the row read again, before the checks of a change that gives
    the role's permissions to its holders (permissions added, restore) or removes it (delete): such changes of one
    role run one after another."""
    query = select(Role).where(Role.id == role_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = await session.exec(query)
    role = result.first()
    if role and role.is_deleted and not include_deleted:
        role = None
    if not role:
        raise ApiError(ErrorCode.ROLE_NOT_FOUND)
    return role


async def get_permission_or_404(session: AsyncSession, permission_id: int) -> Permission:
    """Get permission by ID or raise 404"""
    result = await session.exec(select(Permission).where(Permission.id == permission_id))
    permission = result.first()
    if not permission:
        raise ApiError(ErrorCode.PERMISSION_NOT_FOUND)
    return permission


async def check_role_name_exists(session: AsyncSession, name: str, exclude_id: Optional[int] = None) -> None:
    """Check if role name already exists"""
    query = select(Role).where(Role.name == name)
    if exclude_id:
        query = query.where(Role.id != exclude_id)
    result = await session.exec(query)
    existing = result.first()
    if existing:
        if existing.is_deleted:
            raise ApiError(ErrorCode.ROLE_NAME_ARCHIVED, role_id=existing.id)
        raise ApiError(ErrorCode.ROLE_NAME_TAKEN)


async def permission_names_by_role(session: AsyncSession, role_ids: List[int]) -> Dict[int, Set[str]]:
    """Permission names of every given role, in one query"""
    names: Dict[int, Set[str]] = {}
    if not role_ids:
        return names
    rows = await session.exec(
        select(RolePermission.role_id, Permission.name)
        .join(Permission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(role_ids))
    )
    for role_id, name in rows.all():
        names.setdefault(role_id, set()).add(name)
    return names


# Transitional, until a cleanup revision drops the *_v1 tables: migration 023 keeps the legacy user_roles as
# user_roles_v1 with its foreign key to roles.id (for the downgrade). A role it still names cannot be deleted on MySQL
# while those rows exist, so the permanent delete of a role removes them too (and copies them into its audit row).
LEGACY_ROLE_HOLDERS = "user_roles_v1"


def _has_table(sync_session, name: str) -> bool:
    return sa.inspect(sync_session.connection()).has_table(name)


async def legacy_holder_rows(session: AsyncSession, role_id: int) -> List[Dict[str, Any]]:
    """Every column of the user_roles_v1 rows of the role; none when the table does not exist (a fresh install, or
    after the cleanup revision)"""
    if not await session.run_sync(_has_table, LEGACY_ROLE_HOLDERS):
        return []
    rows = await session.execute(
        sa.text(f"SELECT * FROM {LEGACY_ROLE_HOLDERS} WHERE role_id = :role_id ORDER BY id"), {"role_id": role_id})
    return [dict(row._mapping) for row in rows]


async def delete_legacy_holder_rows(session: AsyncSession, role_id: int) -> None:
    if await session.run_sync(_has_table, LEGACY_ROLE_HOLDERS):
        await session.execute(
            sa.text(f"DELETE FROM {LEGACY_ROLE_HOLDERS} WHERE role_id = :role_id"), {"role_id": role_id})
