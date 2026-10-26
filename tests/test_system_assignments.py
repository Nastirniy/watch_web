"""Authorization v3: system role assignments (GET/POST /users/{user_id}/system-assignments, DELETE
/users/{user_id}/system-assignments/{assignment_id}); the tree assignment API refuses system roles; the last
system_administrator stays (Review Focus 4); type changes give and take system base roles."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.privileges import holds_admin_at_root
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import BuiltinRole, UserType
from app.backend.enums.routes import UserRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from tests.helpers import _permission_names, assign, builtin_role, give, make_admin, make_post, make_user


def system_url(user_id: int) -> str:
    return V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENTS, user_id=user_id)


def system_one(user_id: int, assignment_id: int) -> str:
    return V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENT_BY_ID, user_id=user_id, assignment_id=assignment_id)


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


async def system_rows(db, user) -> list:
    return list((await db.exec(select(SystemRoleAssignment.role_id).where(
        SystemRoleAssignment.user_id == user.id).order_by(SystemRoleAssignment.id))).all())


async def test_list_requires_user_view_covering(client, db, headers):
    admin = await make_admin(db)
    post, other = await make_post(db), await make_post(db)
    target = await make_user(db, post_id=post.id, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), Node.post(post.id))
    await assign(db, target, await builtin_role(db, BuiltinRole.TAG_EDITOR), ROOT)
    listed = await client.get(system_url(target.id), headers=headers(admin))
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert row["role"]["name"] == "tag_editor" and row["role"]["is_builtin"] is True and row["id"] > 0

    outsider = await make_user(db, post_id=other.id, user_type=UserType.ADMINISTRATOR)
    await assign(db, outsider, await builtin_role(db, BuiltinRole.ADMINISTRATOR), Node.post(other.id))
    assert (await client.get(system_url(target.id), headers=headers(outsider))).status_code == 403


async def test_create_tag_editor_for_operator_and_audit(client, db, headers):
    admin = await make_admin(db)
    operator = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, operator, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    editor = await builtin_role(db, BuiltinRole.TAG_EDITOR)
    created = await client.post(system_url(operator.id), json={"role_id": editor.id}, headers=headers(admin))
    assert created.status_code == 201, created.text
    assert created.json()["role"]["id"] == editor.id
    assert await system_rows(db, operator) == [editor.id]

    problem(await client.post(system_url(operator.id), json={"role_id": editor.id}, headers=headers(admin)),
            409, "assignment_exists")
    removed = await client.delete(system_one(operator.id, created.json()["id"]), headers=headers(admin))
    assert removed.status_code == 204, removed.text
    actions = (await db.exec(select(AuditLog.action, AuditLog.old_values).where(
        AuditLog.resource_type == "system_role_assignment").order_by(AuditLog.id))).all()
    assert [action for action, _ in actions] == ["create", "delete"]
    assert actions[1][1]["role_id"] == editor.id and actions[1][1]["user_id"] == operator.id


async def test_create_auditor_for_analyst_refused_by_boundary(client, db, headers):
    admin = await make_admin(db)
    analyst = await make_user(db, user_type=UserType.ANALYST)
    await assign(db, analyst, await builtin_role(db, BuiltinRole.ANALYST), ROOT)
    body = problem(await client.post(system_url(analyst.id), json={"role_id": (await builtin_role(db, BuiltinRole.AUDITOR)).id},
                                     headers=headers(admin)), 409, "role_exceeds_user_type")
    assert body["user_type"] == "analyst" and body["excess_permissions"] == ["audit.view"]


async def test_create_needs_the_permissions_held(client, db, headers):
    operator = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, operator, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    caller = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, caller, await builtin_role(db, BuiltinRole.ADMINISTRATOR), ROOT)
    body = problem(await client.post(system_url(operator.id),
                                     json={"role_id": (await builtin_role(db, BuiltinRole.TAG_EDITOR)).id},
                                     headers=headers(caller)), 403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["tag.create", "tag.update"]


async def test_create_refuses_a_tree_role_and_an_archived_user(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    problem(await client.post(system_url(target.id), json={"role_id": (await builtin_role(db, BuiltinRole.VIEWER)).id},
                              headers=headers(admin)), 409, "role_is_tree")
    target.is_deleted, target.is_active = True, False
    db.add(target)
    await db.commit()
    problem(await client.post(system_url(target.id), json={"role_id": (await builtin_role(db, BuiltinRole.TAG_EDITOR)).id},
                              headers=headers(admin)), 404, "user_not_found")


async def test_system_role_on_tree_assignments_refused(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    editor = await builtin_role(db, BuiltinRole.TAG_EDITOR)
    body = problem(await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id),
                                     json={"role_id": editor.id, "scope": {"kind": "root"}}, headers=headers(admin)),
                   409, "role_is_system")
    assert body["role_id"] == editor.id


async def test_the_last_assignment_counts_both_tables(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.OPERATOR)
    tree = await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    system = await assign(db, target, await builtin_role(db, BuiltinRole.TAG_EDITOR), ROOT)
    removed = await client.delete(V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=target.id, assignment_id=tree.id),
                                  headers=headers(admin))
    assert removed.status_code == 204, removed.text
    problem(await client.delete(system_one(target.id, system.id), headers=headers(admin)), 409, "last_assignment_required")


async def _system_admin_assignment(db, user) -> int:
    role = await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)
    return (await db.exec(select(SystemRoleAssignment.id).where(
        SystemRoleAssignment.user_id == user.id, SystemRoleAssignment.role_id == role.id))).one()


async def _manager(db):
    """Every permission through custom roles: not the administrator (the built-in system_administrator is)"""
    manager = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    system = await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)
    await give(db, manager, sorted(await _permission_names(db, system.id)
                                   | await _permission_names(db, (await builtin_role(db, BuiltinRole.ADMINISTRATOR)).id)))
    return manager


async def test_last_system_administrator_kept_on_delete(client, db, headers):
    """Review Focus 4"""
    only = await make_admin(db)
    manager = await _manager(db)
    assert not await holds_admin_at_root(db, manager.id)
    problem(await client.delete(system_one(only.id, await _system_admin_assignment(db, only)), headers=headers(manager)),
            409, "last_admin_required")


async def test_cannot_remove_own_system_administrator(client, db, headers):
    admin = await make_admin(db)
    await make_admin(db)
    problem(await client.delete(system_one(admin.id, await _system_admin_assignment(db, admin)), headers=headers(admin)),
            409, "cannot_remove_own_admin")


async def test_last_system_administrator_kept_on_archive_deactivate_and_type_change(client, db, headers):
    only = await make_admin(db)
    manager = await _manager(db)
    login = headers(manager)
    problem(await client.post(V1.path(UserRoutes.USER_ARCHIVE, user_id=only.id), headers=login), 409, "last_admin_required")
    user_url = V1.path(UserRoutes.USER_BY_ID, user_id=only.id)
    problem(await client.patch(user_url, data={"is_active": "false"}, headers=login), 409, "last_admin_required")
    problem(await client.patch(user_url, data={"user_type": "administrator"}, headers=login), 409, "last_admin_required")
    assert await holds_admin_at_root(db, only.id)


async def test_type_change_administrator_to_system_administrator_and_back(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    user = await make_user(db, post_id=post.id, user_type=UserType.ADMINISTRATOR)
    administrator = await builtin_role(db, BuiltinRole.ADMINISTRATOR)
    await assign(db, user, administrator, Node.post(post.id))
    user_url = V1.path(UserRoutes.USER_BY_ID, user_id=user.id)

    up = await client.patch(user_url, data={"user_type": "system_administrator"}, headers=headers(admin))
    assert up.status_code == 200, up.text
    tree = (await db.exec(select(RoleAssignment.role_id, RoleAssignment.scope_key).where(
        RoleAssignment.user_id == user.id))).all()
    assert tree == [(administrator.id, "root")]
    assert await system_rows(db, user) == [(await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)).id]

    down = await client.patch(user_url, data={"user_type": "administrator"}, headers=headers(admin))
    assert down.status_code == 200, down.text
    assert await system_rows(db, user) == []
    log = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "user", AuditLog.action == "update",
                                                AuditLog.resource_id == user.id).order_by(AuditLog.id))).all()
    assert log[0].new_values["added_system_assignments"][0]["user_id"] == user.id
    assert log[1].new_values["removed_system_assignments"][0]["user_id"] == user.id
