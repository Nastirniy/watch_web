"""Roles: archive (hidden, not assignable, permissions stop applying, restorable) and the permanent delete."""
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.enums.general import Permissions
from app.backend.enums.routes import RoleRoutes, TagRoutes, AuthRoutes, UserRoutes
from app.backend.api.v1 import V1
from app.backend.models.audit import AuditLog
from app.backend.auth.scope import Node, ROOT
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from tests.helpers import ADMIN_TRUE, assign, make_group, make_post, make_user, make_admin, make_role

ROLES = V1.path(RoleRoutes.ROOT)
ROLE = V1.path(RoleRoutes.ROLE_BY_ID)
ARCHIVE = V1.path(RoleRoutes.ROLE_ARCHIVE)
RESTORE = V1.path(RoleRoutes.ROLE_RESTORE)
TAGS = V1.path(TagRoutes.LIST_ALL)
ROUTE_ACCESS = V1.path(AuthRoutes.ROUTE_ACCESS)


@pytest.mark.asyncio
async def test_archive_lifecycle(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view"], name="reviewer")
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

    # cannot be archived twice, name cannot be reused
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=headers(admin))).status_code == 404
    reuse = await client.post(ROLES, json={"name": "reviewer", "display_name": "Reviewer", "kind": "tree"},
                              headers=headers(admin))
    assert reuse.status_code == 409

    # /auth/me: the assignment is listed with its archived role and grants nothing; the row itself is kept
    me = (await client.get(V1.path(AuthRoutes.ME), headers=headers(user))).json()
    assert [(a["role"]["id"], a["role"]["is_deleted"]) for a in me["assignments"]] == [(role.id, True)]
    assert me["permissions"] == [] and me["post_permissions"] == {} and me["root_permissions"] == []
    assert (await db.exec(select(RoleAssignment).where(RoleAssignment.role_id == role.id))).first() is not None

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
    archiver = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.archive"])
    deleter = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.delete"])

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
    role = await make_role(db, ["tag.view", "post.view"], name="temporary")
    holder, archived_holder = await make_user(db, post_id=post.id), await make_user(db, post_id=post.id)
    for user in (holder, archived_holder):
        await assign(db, user, role, Node.post(post.id))
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

    # unassign the active holder, archive the other one: its assignment row will be removed by the delete (it keeps
    # another assignment, R38)
    await db.execute(RoleAssignment.__table__.delete().where(
        RoleAssignment.user_id == holder.id, RoleAssignment.role_id == role.id))
    archived_holder.is_deleted = True
    db.add(archived_holder)
    await db.commit()
    await assign(db, archived_holder, await make_role(db, ["tag.view"]), Node.post(post.id))
    (removed_row,) = (await db.exec(select(RoleAssignment).where(RoleAssignment.role_id == role.id))).all()
    removed_row.created_by = admin.id
    db.add(removed_row)
    await db.commit()
    expected_assignment = {
        "id": removed_row.id, "user_id": archived_holder.id, "role_id": role.id, "scope_group_id": None,
        "scope_post_id": post.id, "scope_key": f"p:{post.id}", "created_at": removed_row.created_at,
        "created_by": admin.id,
    }

    deleted = await client.delete(url, headers=headers(admin))
    assert deleted.status_code == 204, deleted.text

    assert (await db.exec(select(Role).where(Role.id == role.id))).first() is None
    assert (await db.exec(select(RolePermission).where(RolePermission.role_id == role.id))).all() == []
    assert (await db.exec(select(RoleAssignment).where(RoleAssignment.role_id == role.id))).all() == []
    assert (await client.get(url, headers=headers(admin))).status_code == 404
    assert (await client.delete(url, headers=headers(admin))).status_code == 404
    assert (await client.get(TAGS, headers=headers(holder))).status_code == 403, "nothing is granted any more"

    # name can be reused now
    reuse = await client.post(ROLES, json={"name": "temporary", "display_name": "Temporary", "kind": "tree"},
                              headers=headers(admin))
    assert reuse.status_code == 201, reuse.text

    # the audit row keeps everything that was removed
    log = (await db.exec(select(AuditLog).where(
        AuditLog.resource_type == "role", AuditLog.resource_id == role.id, AuditLog.action == "delete"))).one()
    assert log.old_values["name"] == "temporary"
    assert log.old_values["permissions"] == ["post.view", "tag.view"]
    assert log.old_values["assigned_archived_users"] == [{"id": archived_holder.id, "username": archived_holder.username}]
    # every column of each removed role_assignments row
    (copied,) = log.old_values["assignments"]
    assert set(copied) == set(RoleAssignment.__table__.columns.keys())
    assert {**copied, "created_at": datetime.fromisoformat(copied["created_at"])} == expected_assignment
    assert log.details["permanent"] is True
    assert log.old_values["user_roles_v1"] == [], "no user_roles_v1 table (a fresh install): nothing to copy"


@pytest.mark.asyncio
async def test_permanent_delete_never_leaves_a_holder_without_an_assignment(client, db, headers):
    """R38: the delete removes the assignments of archived holders; one whose only assignment is of the role would
    come back from a restore without any (rule 6): 409 last_assignment_required, nothing removed. With another
    assignment of the holder the delete goes through."""
    post = await make_post(db)
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view"])
    holder = await make_user(db, post_id=post.id)
    only = await assign(db, holder, role, Node.post(post.id))
    login = headers(admin)
    archived = await client.post(V1.path(UserRoutes.USER_ARCHIVE, user_id=holder.id), headers=login)
    assert archived.status_code == 200, archived.text
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=login)).status_code == 200

    refused = await client.delete(ROLE.format(role_id=role.id), headers=login)
    assert refused.status_code == 409 and refused.json()["code"] == "last_assignment_required", refused.text
    assert (await db.exec(select(Role.id).where(Role.id == role.id))).first() == role.id
    assert (await db.exec(select(RoleAssignment.id).where(RoleAssignment.role_id == role.id))).all() == [only.id]
    assert (await db.exec(select(RolePermission).where(RolePermission.role_id == role.id))).first() is not None
    assert not (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role",
                                                     AuditLog.action == "delete"))).all()

    other = await assign(db, holder, await make_role(db, ["tag.view"]), Node.post(post.id))
    deleted = await client.delete(ROLE.format(role_id=role.id), headers=login)
    assert deleted.status_code == 204, deleted.text
    left = (await db.exec(select(RoleAssignment.id).where(RoleAssignment.user_id == holder.id))).all()
    assert left == [other.id]


LEGACY_HOLDERS_DDL = (  # user_roles_v1 as 023 leaves it: the legacy user_roles, foreign key to roles.id kept
    "CREATE TABLE user_roles_v1 (id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id),"
    " role_id INTEGER NOT NULL REFERENCES roles (id), assigned_at DATETIME NOT NULL,"
    " CONSTRAINT uq_user_roles_user_role UNIQUE (user_id, role_id))"
)


@pytest.mark.asyncio
async def test_permanent_delete_removes_legacy_holder_rows(client, db, headers):
    """A legacy role archived before the upgrade can still have rows in user_roles_v1 (023 keeps the table with its
    foreign key; MySQL refused the delete with a 500): they go with the role, and the audit row keeps a full copy"""
    admin = await make_admin(db)
    holder, other = await make_user(db), await make_user(db)
    role = await make_role(db, ["tag.view"], name="legacy_archived")
    kept = await make_role(db, ["tag.view"], name="legacy_kept")
    await db.execute(text(LEGACY_HOLDERS_DDL))
    await db.execute(text("INSERT INTO user_roles_v1 (id, user_id, role_id, assigned_at) VALUES"
                          " (5, :holder, :role, '2026-01-01 00:00:00'), (6, :other, :kept, '2026-01-02 00:00:00')"),
                     {"holder": holder.id, "other": other.id, "role": role.id, "kept": kept.id})
    await db.commit()
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=headers(admin))).status_code == 200

    deleted = await client.delete(ROLE.format(role_id=role.id), headers=headers(admin))
    assert deleted.status_code == 204, deleted.text

    left = (await db.execute(text("SELECT id, role_id FROM user_roles_v1 ORDER BY id"))).all()
    assert [tuple(row) for row in left] == [(6, kept.id)]
    log = (await db.exec(select(AuditLog).where(
        AuditLog.resource_type == "role", AuditLog.resource_id == role.id, AuditLog.action == "delete"))).one()
    assert log.old_values["user_roles_v1"] == [
        {"id": 5, "user_id": holder.id, "role_id": role.id, "assigned_at": "2026-01-01 00:00:00"}]


@pytest.mark.asyncio
async def test_admin_role_can_neither_be_archived_nor_deleted(client, db, headers):
    admin = await make_admin(db)
    admin_role = (await db.exec(select(Role).where(Role.name == "system_administrator"))).one()
    assert (await client.post(ARCHIVE.format(role_id=admin_role.id), headers=headers(admin))).status_code == 409
    assert (await client.delete(ROLE.format(role_id=admin_role.id), headers=headers(admin))).status_code == 409
    assert (await client.get(ROLE.format(role_id=admin_role.id), headers=headers(admin))).json()["is_deleted"] is False


@pytest.mark.asyncio
async def test_seed_restores_deleted_admin_role(db):
    admin_role = (await db.exec(select(Role).where(Role.name == "system_administrator"))).one()
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
    group, post = await make_group(db), await make_post(db)
    role_id, user_id, group_id, post_id = role.id, user.id, group.id, post.id
    for node in (ROOT, Node.group(group_id), Node.post(post_id)):
        db.add(RoleAssignment(user_id=user_id, role_id=role_id, **scope_columns(node)))
    await db.commit()
    for node in (ROOT, Node.group(group_id), Node.post(post_id)):   # one role at one node per user
        db.add(RoleAssignment(user_id=user_id, role_id=role_id, **scope_columns(node)))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    # never a group and a post at once
    db.add(RoleAssignment(user_id=user_id, role_id=role_id, scope_group_id=group_id, scope_post_id=post_id,
                          scope_key="g:0"))
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
