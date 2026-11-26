"""Authorization v3, the deferred minors of the final review: the items of user_type_ceiling_exceeded name the table
of their assignment (kind), a type change checks every assignment against the boundary of its own kind, and a type
change locks the roles of the user before any plain read (a concurrent permission add to one of them is seen whole)."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1 import users as users_routes
from app.backend.auth.scope import ROOT
from app.backend.enums.general import BuiltinRole, RoleKind, UserType
from app.backend.enums.routes import UserRoutes
from app.backend.models.user import User
from tests.helpers import assign, builtin_role, make_admin, make_role, make_user


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.json()
    return response.json()


async def change_type(client, login: dict, target: User, user_type: str):
    return await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), data={"user_type": user_type},
                              headers=login)


async def test_ceiling_items_name_their_kind(client, db, headers):
    """A system assignment over the new type is listed with kind system, a tree one with kind tree"""
    admin = await make_admin(db)
    operator = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, operator, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    system = await assign(db, operator, await builtin_role(db, BuiltinRole.TAG_EDITOR), ROOT)
    tree = await assign(db, operator, await make_role(db, ["media.manage_any"]), ROOT)

    body = problem(await change_type(client, headers(admin), operator, "analyst"), 409, "user_type_ceiling_exceeded")
    assert {(item["kind"], item["id"]) for item in body["assignments"]} == {("system", system.id), ("tree", tree.id)}


async def test_ceiling_checks_each_assignment_against_its_own_boundary(client, db, headers):
    """A tree grant puts only tree permissions in effect: a system permission in a tree role is outside the tree
    boundary of the new type, though its system boundary holds it"""
    admin = await make_admin(db)
    viewer = await make_user(db, user_type=UserType.VIEWER)
    await assign(db, viewer, await builtin_role(db, BuiltinRole.VIEWER), ROOT)
    misplaced = await assign(db, viewer, await make_role(db, ["tag.create"], kind=RoleKind.TREE), ROOT)

    body = problem(await change_type(client, headers(admin), viewer, "operator"), 409, "user_type_ceiling_exceeded")
    assert [(item["kind"], item["id"], item["excess_permissions"]) for item in body["assignments"]] == [
        ("tree", misplaced.id, ["tag.create"])]


async def test_type_change_locks_the_roles_of_the_user_first(client, db, headers, monkeypatch):
    """The roles of the user are locked (shared) before the user entity, whose presence subqueries take the MariaDB
    snapshot: a permission added to one of them meanwhile is read as committed"""
    admin = await make_admin(db)
    senior = await make_user(db, user_type=UserType.SENIOR_OPERATOR)
    await assign(db, senior, await builtin_role(db, BuiltinRole.SENIOR_OPERATOR), ROOT)
    custom = await make_role(db, ["observation.view"])
    await assign(db, senior, custom, ROOT)

    calls = []
    original_lock, original_all = users_routes.lock_row, users_routes.lock_all

    async def recording_lock(session, query, shared=False):
        calls.append(("one", "entity" if query.column_descriptions[0]["expr"] is User else "columns", shared))
        return await original_lock(session, query, shared=shared)

    async def recording_all(session, query, shared=False):
        calls.append(("all", query.column_descriptions[0]["entity"].__name__, shared))
        return await original_all(session, query, shared=shared)

    monkeypatch.setattr(users_routes, "lock_row", recording_lock)
    monkeypatch.setattr(users_routes, "lock_all", recording_all)
    changed = await change_type(client, headers(admin), senior, "operator")
    assert changed.status_code == 200, changed.text

    entity = next(i for i, call in enumerate(calls) if call[:2] == ("one", "entity"))
    roles = next(i for i, call in enumerate(calls) if call == ("all", "Role", True))
    assert roles < entity, calls
    stored = (await db.exec(select(User.user_type).where(User.id == senior.id))).one()
    assert stored == "operator"
