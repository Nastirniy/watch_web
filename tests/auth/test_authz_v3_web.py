"""Authorization v3, what the web client reads: system assignments and permissions in /auth/me and the login
response, root_permissions of tree grants only, the base roles and both boundaries of each type in /user-types."""
from app.backend.api.v1 import V1
from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.enums.general import PermissionLevel
from app.backend.enums.routes import AuthRoutes, UserTypeRoutes
from tests.helpers import make_admin, make_user

ME = V1.path(AuthRoutes.ME)
SYSTEM = sorted(name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.SYSTEM)
TREE = sorted(name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.TREE)


async def test_me_of_a_system_administrator(client, db, headers):
    admin = await make_admin(db)
    body = (await client.get(ME, headers=headers(admin))).json()
    assert body["system_permissions"] == SYSTEM
    assert body["root_permissions"] == TREE
    assert body["permissions"] == sorted(TREE + SYSTEM)
    (system,) = body["system_assignments"]
    assert system["role"]["name"] == "system_administrator" and system["id"] > 0
    (tree,) = body["assignments"]
    assert tree["role"]["name"] == "administrator" and tree["scope"]["kind"] == "root"


async def test_me_without_system_roles(client, db, headers):
    body = (await client.get(ME, headers=headers(await make_user(db, perms=["post.view"])))).json()
    assert body["system_permissions"] == [] and body["system_assignments"] == []


async def test_user_types_list_base_roles_and_boundaries(client, db, headers):
    body = (await client.get(V1.path(UserTypeRoutes.ROOT), headers=headers(await make_user(db)))).json()
    by_key = {t["key"]: t for t in body}
    operator = by_key["operator"]
    assert operator["system_boundary"] == ["tag.create", "tag.update"]
    assert [(r["name"], r["kind"], r["at_root"]) for r in operator["base_roles"]] == [("operator", "tree", False)]
    assert "observation.manage_any" in operator["tree_boundary"]
    system_admin = by_key["system_administrator"]
    assert [(r["name"], r["kind"], r["at_root"]) for r in system_admin["base_roles"]] == [
        ("administrator", "tree", True), ("system_administrator", "system", True)]
    assert system_admin["system_boundary"] == SYSTEM and system_admin["tree_boundary"] == TREE
    assert all(r["id"] > 0 for t in body for r in t["base_roles"])
