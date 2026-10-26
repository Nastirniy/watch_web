"""Authorization v3: roles of two kinds in the API. A role holds permissions of one level (tree or system), its kind
is set at creation and never changes; a permission of the other level is refused (409 permission_level_mismatch)."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import ROOT
from app.backend.enums.general import UserType
from app.backend.enums.routes import RoleRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role import Permission, Role
from app.backend.models.system_role_assignment import SystemRoleAssignment
from tests.helpers import assign, give, make_admin, make_role, make_user

ROLES = V1.path(RoleRoutes.ROOT)
ROLE = V1.path(RoleRoutes.ROLE_BY_ID)
CLONE = V1.path(RoleRoutes.ROLE_CLONE)
ARCHIVE = V1.path(RoleRoutes.ROLE_ARCHIVE)
ROLE_PERMISSION = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID)
ALL_PERMISSIONS = V1.path(RoleRoutes.ALL_PERMISSIONS)


async def permission_id(db, name: str) -> int:
    return (await db.exec(select(Permission.id).where(Permission.name == name))).one()


def mismatch(response, role_kind: str, permissions: list) -> None:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "permission_level_mismatch" and body["role_kind"] == role_kind
    assert body["permissions"] == permissions


async def test_create_requires_kind(client, db, headers):
    response = await client.post(ROLES, headers=headers(await make_admin(db)),
                                 json={"name": "nokind", "display_name": "No kind"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "#/kind"


async def test_create_with_a_permission_of_the_other_level_refused(client, db, headers):
    h = headers(await make_admin(db))
    view, audit = await permission_id(db, "post.view"), await permission_id(db, "audit.view")
    mismatch(await client.post(ROLES, headers=h, json={"name": "s", "display_name": "S", "kind": "system",
                                                       "permission_ids": [audit, view]}), "system", ["post.view"])
    mismatch(await client.post(ROLES, headers=h, json={"name": "t", "display_name": "T", "kind": "tree",
                                                       "permission_ids": [audit, view]}), "tree", ["audit.view"])
    assert (await db.exec(select(Role).where(Role.name.in_(["s", "t"])))).first() is None
    created = await client.post(ROLES, headers=h, json={"name": "auditors", "display_name": "Auditors", "kind": "system",
                                                        "permission_ids": [audit]})
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "system"


async def test_patch_and_put_refuse_the_other_level(client, db, headers):
    h = headers(await make_admin(db))
    role = await make_role(db, ["post.view"])
    audit = await permission_id(db, "audit.view")
    mismatch(await client.patch(ROLE.format(role_id=role.id), headers=h,
                                json={"permission_ids": [await permission_id(db, "post.view"), audit]}),
             "tree", ["audit.view"])
    mismatch(await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=audit), headers=h),
             "tree", ["audit.view"])


async def test_clone_keeps_kind(client, db, headers):
    h = headers(await make_admin(db))
    source = await make_role(db, ["audit.view"])
    cloned = await client.post(CLONE.format(role_id=source.id), headers=h, json={"name": "copy", "display_name": "Copy"})
    assert cloned.status_code == 201, cloned.text
    assert cloned.json()["kind"] == "system"


async def test_list_filters_by_kind(client, db, headers):
    h = headers(await make_admin(db))
    tree, system = await make_role(db, ["post.view"]), await make_role(db, ["audit.view"])
    listed = (await client.get(ROLES, params={"kind": "system", "is_builtin": "false"}, headers=h)).json()
    assert [r["id"] for r in listed] == [system.id]
    listed = (await client.get(ROLES, params={"kind": "tree", "is_builtin": "false"}, headers=h)).json()
    assert [r["id"] for r in listed] == [tree.id]


async def test_list_all_permissions_levels_are_tree_or_system(client, db, headers):
    body = (await client.get(ALL_PERMISSIONS, headers=headers(await make_admin(db)))).json()
    assert {p["level"] for p in body} == {"tree", "system"}
    assert {p["name"]: p["level"] for p in body}["database.view"] == "system"


async def test_adding_to_a_held_system_role_checks_the_holders_boundary(client, db, headers):
    h = headers(await make_admin(db))
    operator = await make_user(db, user_type=UserType.OPERATOR)
    (taggers,) = await give(db, operator, ["tag.create"])
    response = await client.put(ROLE_PERMISSION.format(role_id=taggers.id,
                                                       permission_id=await permission_id(db, "audit.view")), headers=h)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "role_holders_exceed_user_type"
    assert body["users"] == [{"id": operator.id, "username": operator.username, "user_type": "operator",
                              "excess_permissions": ["audit.view"]}]


async def test_hard_delete_of_a_system_role_removes_its_system_assignments(client, db, headers):
    """Review Focus 3"""
    h = headers(await make_admin(db))
    holder = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await give(db, holder, ["post.view"])             # another assignment: the holder keeps one
    role = await make_role(db, ["audit.view"])
    row = await assign(db, holder, role, ROOT)
    holder.is_deleted, holder.is_active = True, False
    db.add(holder)
    await db.commit()
    assert (await client.post(ARCHIVE.format(role_id=role.id), headers=h)).status_code == 200

    deleted = await client.delete(ROLE.format(role_id=role.id), headers=h)
    assert deleted.status_code == 204, deleted.text
    assert (await db.exec(select(SystemRoleAssignment).where(SystemRoleAssignment.id == row.id))).first() is None
    log = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role", AuditLog.action == "delete"))).one()
    assert [a["id"] for a in log.old_values["system_assignments"]] == [row.id]
    assert log.old_values["system_assignments"][0]["user_id"] == holder.id
