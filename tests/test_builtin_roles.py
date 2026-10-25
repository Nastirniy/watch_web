"""Built-in roles: the catalog of auth/builtin_roles.py, seeded and re-synced at every start, read-only in the API,
clonable."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.enums.general import BuiltinRole
from app.backend.enums.routes import RoleRoutes, SearchRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role import Permission, Role, RolePermission
from tests.helpers import make_admin, make_role, make_user

ROLES = V1.path(RoleRoutes.ROOT)
ROLE = V1.path(RoleRoutes.ROLE_BY_ID)
ARCHIVE = V1.path(RoleRoutes.ROLE_ARCHIVE)
CLONE = V1.path(RoleRoutes.ROLE_CLONE)
ROLE_PERMISSION = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID)


async def builtin(db, key: str) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == key))).one()


async def permission_names(db, role_id: int) -> set:
    rows = await db.exec(
        select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id))
    return set(rows.all())


async def permission_id(db, name: str) -> int:
    return (await db.exec(select(Permission.id).where(Permission.name == name))).one()


async def test_seed_creates_and_resyncs_builtin_roles(db):
    for key, spec in BUILTIN_ROLES.items():
        role = await builtin(db, key.value)
        assert role.name == key.value and role.display_name == spec.display_name and not role.is_deleted
        assert role.kind == spec.kind.value
        assert await permission_names(db, role.id) == spec.permissions, key

    viewer, operator = await builtin(db, "viewer"), await builtin(db, "operator")
    db.add(RolePermission(role_id=viewer.id, permission_id=await permission_id(db, "user.update")))
    dropped = await permission_id(db, "media.create")
    await db.execute(RolePermission.__table__.delete().where(
        RolePermission.role_id == operator.id, RolePermission.permission_id == dropped))
    operator.display_name = "changed"
    operator.is_deleted = True
    db.add(operator)
    await db.commit()

    await seed_roles_and_permissions(db)
    await db.refresh(operator)
    assert operator.display_name == BUILTIN_ROLES[BuiltinRole.OPERATOR].display_name and operator.is_deleted is False
    for key, spec in BUILTIN_ROLES.items():
        assert await permission_names(db, (await builtin(db, key.value)).id) == spec.permissions, key
    assert len((await db.exec(select(Role).where(Role.builtin_key.is_not(None)))).all()) == len(BUILTIN_ROLES)


async def test_seed_claims_a_role_named_like_a_key(db):
    custom = await make_role(db, ["tag.view"], name="analyst_old")
    await db.execute(Role.__table__.update().where(Role.id == (await builtin(db, "analyst")).id).values(builtin_key=None))
    await db.commit()
    await seed_roles_and_permissions(db)
    analyst = await builtin(db, "analyst")
    assert analyst.name == "analyst" and custom.id != analyst.id


async def test_builtin_roles_refuse_changes(client, db, headers):
    h = headers(await make_admin(db))
    role = await builtin(db, "operator")
    before = await permission_names(db, role.id)
    target = ROLE.format(role_id=role.id)

    r = await client.patch(target, headers=h, json={"name": "night"})
    assert r.status_code == 409 and r.json()["code"] == "builtin_role_not_renamable"
    r = await client.patch(target, headers=h, json={"permission_ids": [await permission_id(db, "tag.view")]})
    assert r.status_code == 409 and r.json()["code"] == "builtin_role_permissions_fixed"
    r = await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=await permission_id(db, "user.view")),
                         headers=h)
    assert r.status_code == 409 and r.json()["code"] == "builtin_role_permissions_fixed"
    r = await client.delete(ROLE_PERMISSION.format(role_id=role.id, permission_id=await permission_id(db, "tag.view")),
                            headers=h)
    assert r.status_code == 409 and r.json()["code"] == "builtin_role_permissions_fixed"
    r = await client.post(ARCHIVE.format(role_id=role.id), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "builtin_role_not_archivable"
    r = await client.delete(target, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "builtin_role_not_deletable"

    assert await permission_names(db, role.id) == before
    # the unchanged name and the texts stay editable
    r = await client.patch(target, headers=h, json={"name": "operator", "description": "note"})
    assert r.status_code == 200, r.text


async def test_clone_builtin_role(client, db, headers):
    admin = await make_admin(db)
    operator = await builtin(db, "operator")
    r = await client.post(CLONE.format(role_id=operator.id), headers=headers(admin),
                          json={"name": "operator_night", "display_name": "Night operator"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_builtin"] is False and body["builtin_key"] is None and body["name"] == "operator_night"
    assert await permission_names(db, body["id"]) == await permission_names(db, operator.id)

    log = (await db.exec(select(AuditLog).where(
        AuditLog.resource_type == "role", AuditLog.resource_id == body["id"], AuditLog.action == "create"))).one()
    assert log.details["cloned_from"] == operator.id

    taken = await client.post(CLONE.format(role_id=operator.id), headers=headers(admin),
                              json={"name": "operator_night", "display_name": "Again"})
    assert taken.status_code == 400 and taken.json()["code"] == "role_name_taken"

    # a clone is an ordinary role
    assert (await client.patch(ROLE.format(role_id=body["id"]), headers=headers(admin), json={"name": "night"})).status_code == 200

    limited = await make_user(db, perms=["admin.true", "role.view", "role.create", "tag.view"])
    denied = await client.post(CLONE.format(role_id=operator.id), headers=headers(limited),
                               json={"name": "operator_copy", "display_name": "Copy"})
    assert denied.status_code == 403 and denied.json()["code"] == "grant_exceeds_own_permissions"
    assert "observation.create" in denied.json()["missing"]


async def test_role_response_level_and_builtin_filter(client, db, headers):
    h = headers(await make_admin(db))
    custom = await make_role(db, ["observation.view"], name="only_view")
    mixed = await make_role(db, ["observation.view", "tag.create"], name="view_and_tags", allow_mixed=True)

    admin_body = (await client.get(ROLE.format(role_id=(await builtin(db, "system_administrator")).id), headers=h)).json()
    assert admin_body["is_builtin"] is True and admin_body["builtin_key"] == "system_administrator"
    assert admin_body["level"] == "system"
    assert (await client.get(ROLE.format(role_id=custom.id), headers=h)).json()["level"] == "tree"
    assert (await client.get(ROLE.format(role_id=mixed.id), headers=h)).json()["level"] == "system"
    assert (await client.get(ROLE.format(role_id=custom.id), headers=h)).json()["is_builtin"] is False

    custom_only = (await client.get(ROLES, params={"is_builtin": "false"}, headers=h)).json()
    assert {r["id"] for r in custom_only} == {custom.id, mixed.id}
    built_in = (await client.get(ROLES, params={"is_builtin": "true"}, headers=h)).json()
    assert len(built_in) == len(BuiltinRole) and all(r["is_builtin"] for r in built_in)
    levels = {r["name"]: r["level"] for r in built_in}
    assert levels["viewer"] == "tree" and levels["system_administrator"] == "system"

    permissions = (await client.get(V1.path(RoleRoutes.ALL_PERMISSIONS), headers=h)).json()
    assert {p["name"]: p["level"] for p in permissions}["tag.create"] == "system"
    assert {p["name"]: p["level"] for p in permissions}["tag.view"] == "tree"


def test_role_fields_are_required_in_the_schema():
    """Always present in every role and permission: openapi-typescript types them as non-optional"""
    from app.main import fastapi_app
    schemas = V1.openapi(fastapi_app)["components"]["schemas"]
    assert {"builtin_key", "is_builtin", "level"} <= set(schemas["RoleResponse"]["required"])
    assert "level" in schemas["PermissionResponse"]["required"]


async def test_search_roles_reports_builtin_and_level(client, db, headers):
    h = headers(await make_admin(db))
    body = (await client.get(V1.path(SearchRoutes.ROLES), params={"is_builtin": "true"}, headers=h)).json()
    items = {r["name"]: r for r in body["items"]}
    assert set(items) == {key.value for key in BuiltinRole}
    assert items["system_administrator"]["is_builtin"] is True and items["system_administrator"]["level"] == "system"
    assert items["viewer"]["level"] == "tree"
