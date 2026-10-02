"""Roles: archive (hidden, not assignable, permissions stop applying, restorable) and the permanent delete."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.backend.database import seed_roles_and_permissions
from app.backend.enums.general import Permissions
from app.backend.enums.routes import RoleRoutes, TagRoutes, UserRoutes, AuthRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role import Role, UserRole, Permission, RolePermission
from tests.helpers import make_post, make_user, make_admin, make_role

ROLES = RoleRoutes.ROOT.versioned_path()
ROLE = RoleRoutes.ROLE_BY_ID.versioned_path()
ARCHIVE = RoleRoutes.ROLE_ARCHIVE.versioned_path()
RESTORE = RoleRoutes.ROLE_RESTORE.versioned_path()
TAGS = TagRoutes.LIST_ALL.versioned_path()
ROUTE_ACCESS = AuthRoutes.ROUTE_ACCESS.versioned_path()


@pytest.mark.asyncio
async def test_archive_lifecycle(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view"], name="viewer")
    user = await make_user(db, role=role, post_id=post.id)

    assert (await client.get(TAGS, headers=headers(user))).status_code == 200
    assert (await client.post(RESTORE.format(role_id=role.id), headers=headers(admin))).status_code == 409, "not archived yet"

    archived = await client.post(ARCHIVE.format(role_id=role.id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert archived.json()["id"] == role.id and archived.json()["is_deleted"] is True

    # permissions stop applying immediately
    denied = await client.get(TAGS, headers=headers(user))
    assert denied.status_code == 403
    assert denied.json()["missing"] == ["tag.view"]

    # hidden from lists unless include_deleted
    listed = (await client.get(ROLES, headers=headers(admin))).json()
    assert role.id not in {r["id"] for r in listed}
    with_deleted = (await client.get(ROLES, params={"include_deleted": "true"}, headers=headers(admin))).json()
    assert {r["id"]: r["is_deleted"] for r in with_deleted}[role.id] is True

    # still inspectable by id for the administration GUI, but read-only
    detail = await client.get(ROLE.format(role_id=role.id), headers=headers(admin))
    assert detail.status_code == 200 and detail.json()["is_deleted"] is True
    assert (await client.patch(ROLE.format(role_id=role.id), json={"display_name": "x"}, headers=headers(admin))).status_code == 404
    assert (await client.patch(ROLE.format(role_id=role.id), json={"is_deleted": False}, headers=headers(admin))).status_code == 422, "is_deleted left the body"

    # cannot be archived twice, cannot be assigned, name cannot be reused
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=headers(admin))).status_code == 404
    assign = await client.patch(UserRoutes.USER_BY_ID.versioned_path().format(user_id=user.id),
                                data={"role_ids": f"[{role.id}]"}, headers=headers(admin))
    assert assign.status_code == 400, assign.text
    reuse = await client.post(ROLES, json={"name": "viewer", "display_name": "Viewer"}, headers=headers(admin))
    assert reuse.status_code == 409

    # the user's role list hides it, /auth/me shows no roles; the assignment row itself is kept
    user_roles = (await client.get(UserRoutes.USER_ROLES.versioned_path().format(user_id=user.id), headers=headers(admin))).json()
    assert user_roles == []
    me = (await client.get(AuthRoutes.ME.versioned_path(), headers=headers(user))).json()
    assert me["roles"] == [] and me["permissions"] == []
    assert (await db.exec(select(UserRole).where(UserRole.role_id == role.id))).first() is not None

    # restore
    restored = await client.post(RESTORE.format(role_id=role.id), headers=headers(admin))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False
    assert (await client.get(TAGS, headers=headers(user))).status_code == 200
    assert role.id in {r["id"] for r in (await client.get(ROLES, headers=headers(admin))).json()}

    actions = (await db.exec(select(AuditLog.action).where(AuditLog.resource_type == "role", AuditLog.resource_id == role.id))).all()
    assert actions == ["archive", "restore"]


@pytest.mark.asyncio
async def test_archive_requires_role_archive_not_role_delete(client, db, headers):
    role = await make_role(db, [])
    archiver = await make_user(db, perms=["admin.true", "role.view", "role.archive"])
    deleter = await make_user(db, perms=["admin.true", "role.view", "role.delete"])

    denied = await client.post(ARCHIVE.format(role_id=role.id), headers=headers(deleter))
    assert denied.status_code == 403 and denied.json()["missing"] == ["role.archive"]
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=headers(archiver))).status_code == 200

    denied = await client.delete(ROLE.format(role_id=role.id), headers=headers(archiver))
    assert denied.status_code == 403 and denied.json()["missing"] == ["role.delete"]

    # the route access map shows the permanent delete only to holders of role.delete
    listed = {(r["method"], r["path"]) for r in (await client.get(ROUTE_ACCESS, headers=headers(archiver))).json()["routes"]}
    assert ("DELETE", ROLE) not in listed and ("POST", ARCHIVE) in listed
    listed = {(r["method"], r["path"]) for r in (await client.get(ROUTE_ACCESS, headers=headers(deleter))).json()["routes"]}
    assert ("DELETE", ROLE) in listed and ("POST", ARCHIVE) not in listed


@pytest.mark.asyncio
async def test_permanent_delete_preconditions_and_effect(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view", "tag.create"], name="temporary")
    holder = await make_user(db, role=role, post_id=post.id)
    archived_holder = await make_user(db, role=role, post_id=post.id)
    url = ROLE.format(role_id=role.id)

    # must be archived first
    live = await client.delete(url, headers=headers(admin))
    assert live.status_code == 409 and live.json()["code"] == "role_delete_requires_archive"
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=headers(admin))).status_code == 200

    # no active user may hold it; the answer lists them
    blocked = await client.delete(url, headers=headers(admin))
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["assigned_users"] == [
        {"id": holder.id, "username": holder.username},
        {"id": archived_holder.id, "username": archived_holder.username},
    ]

    # unassign the active holder, archive the other one: its assignment row will be removed by the delete
    await db.execute(UserRole.__table__.delete().where(UserRole.user_id == holder.id, UserRole.role_id == role.id))
    archived_holder.is_deleted = True
    db.add(archived_holder)
    await db.commit()

    deleted = await client.delete(url, headers=headers(admin))
    assert deleted.status_code == 204, deleted.text

    assert (await db.exec(select(Role).where(Role.id == role.id))).first() is None
    assert (await db.exec(select(RolePermission).where(RolePermission.role_id == role.id))).all() == []
    assert (await db.exec(select(UserRole).where(UserRole.role_id == role.id))).all() == []
    assert (await client.get(url, headers=headers(admin))).status_code == 404
    assert (await client.delete(url, headers=headers(admin))).status_code == 404
    assert (await client.get(TAGS, headers=headers(holder))).status_code == 403, "nothing is granted any more"

    # name can be reused now
    reuse = await client.post(ROLES, json={"name": "temporary", "display_name": "Temporary"}, headers=headers(admin))
    assert reuse.status_code == 201, reuse.text

    # the audit row keeps everything that was removed
    log = (await db.exec(select(AuditLog).where(
        AuditLog.resource_type == "role", AuditLog.resource_id == role.id, AuditLog.action == "delete"))).one()
    assert log.old_values["name"] == "temporary"
    assert log.old_values["permissions"] == ["tag.create", "tag.view"]
    assert log.old_values["assigned_archived_users"] == [{"id": archived_holder.id, "username": archived_holder.username}]
    assert log.details["permanent"] is True


@pytest.mark.asyncio
async def test_admin_role_can_neither_be_archived_nor_deleted(client, db, headers):
    admin = await make_admin(db)
    admin_role = (await db.exec(select(Role).where(Role.name == "admin"))).one()
    assert (await client.post(ARCHIVE.format(role_id=admin_role.id), headers=headers(admin))).status_code == 409
    assert (await client.delete(ROLE.format(role_id=admin_role.id), headers=headers(admin))).status_code == 409
    assert (await client.get(ROLE.format(role_id=admin_role.id), headers=headers(admin))).json()["is_deleted"] is False


@pytest.mark.asyncio
async def test_seed_restores_deleted_admin_role(db):
    admin_role = (await db.exec(select(Role).where(Role.name == "admin"))).one()
    admin_role.is_deleted = True
    db.add(admin_role)
    await db.commit()
    await seed_roles_and_permissions(db)
    await db.refresh(admin_role)
    assert admin_role.is_deleted is False


@pytest.mark.asyncio
async def test_seed_refreshes_permission_texts(db):
    row = (await db.exec(select(Permission).where(Permission.name == Permissions.ROLE_DELETE.value))).one()
    original = (row.display_name, row.description)
    assert original[0].startswith("Остаточне") and "НЕБЕЗПЕЧНО" in original[1]
    row.display_name, row.description = "stale", "stale"
    db.add(row)
    await db.commit()
    await seed_roles_and_permissions(db)
    await db.refresh(row)
    assert (row.display_name, row.description) == original


@pytest.mark.asyncio
async def test_assignments_are_unique(db):
    role = await make_role(db, [])
    user = await make_user(db, role=role)
    role_id, user_id = role.id, user.id
    db.add(UserRole(user_id=user_id, role_id=role_id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    permission_id = (await db.exec(select(Permission.id).where(Permission.name == "tag.view"))).one()
    db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    await db.commit()
    db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
