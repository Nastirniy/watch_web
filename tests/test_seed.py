"""Seeding: the admin role always ends up with every permission."""
import pytest
from sqlmodel import select

from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.enums.general import Permissions
from app.backend.models.role import Role, Permission, RolePermission


async def admin_grants(db) -> set:
    admin = (await db.exec(select(Role).where(Role.name == "admin"))).one()
    return set((await db.exec(select(RolePermission.permission_id).where(RolePermission.role_id == admin.id))).all())


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_complete(db):
    permissions = set((await db.exec(select(Permission.id))).all())
    assert len(permissions) == len(list(Permissions))
    assert await admin_grants(db) == permissions

    await seed_roles_and_permissions(db)
    assert set((await db.exec(select(Permission.id))).all()) == permissions
    assert await admin_grants(db) == permissions


@pytest.mark.asyncio
async def test_seed_regrants_removed_permission(db):
    admin = (await db.exec(select(Role).where(Role.name == "admin"))).one()
    admin_perm = (await db.exec(select(Permission).where(Permission.name == Permissions.IS_ADMIN.value))).one()
    row = (await db.exec(select(RolePermission).where(
        RolePermission.role_id == admin.id, RolePermission.permission_id == admin_perm.id))).one()
    await db.delete(row)
    await db.commit()
    assert admin_perm.id not in await admin_grants(db)

    await seed_roles_and_permissions(db)
    assert admin_perm.id in await admin_grants(db)
