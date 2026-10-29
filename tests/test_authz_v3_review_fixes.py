"""Authorization v3, fixes of the final review: the role row is locked before any plain read of an assignment write
(R40), the administrator is a user holding system_administrator AND administrator at the root, a grant puts nothing
in effect when its role's kind differs from its table."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth import assignments as assignment_rules
from app.backend.auth import privileges
from app.backend.auth import system_assignments as system_rules
from app.backend.auth.integrity import authorization_report
from app.backend.auth.privileges import holds_admin_at_root
from app.backend.auth.scope import ROOT
from app.backend.enums.general import BuiltinRole, RoleKind, UserType
from app.backend.enums.routes import DatabaseRoutes, UserRoutes
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from tests.helpers import assign, builtin_role, give, make_admin, make_role, make_user


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.json()
    return response.json()


def _record_order(monkeypatch, module):
    """The order of the role row lock (lock) and the first plain read of grants (every_assignment, the target's) in an
    assignment write"""
    calls = []
    original_lock, original_grants = module.lock, privileges.every_assignment

    async def recording_lock(session, query, shared=False):
        calls.append("lock")
        return await original_lock(session, query, shared=shared)

    async def recording_grants(session, user):
        calls.append("grants")
        return await original_grants(session, user)

    monkeypatch.setattr(module, "lock", recording_lock)
    monkeypatch.setattr(privileges, "every_assignment", recording_grants)
    return calls


async def test_tree_assignment_locks_the_role_before_reading(client, db, headers, monkeypatch):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    role = await make_role(db, ["post.view"])
    calls = _record_order(monkeypatch, assignment_rules)
    created = await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id),
                                json={"role_id": role.id, "scope": {"kind": "root"}}, headers=headers(admin))
    assert created.status_code == 201, created.text
    assert "lock" in calls and "grants" in calls and calls.index("lock") < calls.index("grants"), calls


async def test_system_assignment_locks_the_role_before_reading(client, db, headers, monkeypatch):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), ROOT)
    calls = _record_order(monkeypatch, system_rules)
    created = await client.post(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENTS, user_id=target.id),
                                json={"role_id": (await builtin_role(db, BuiltinRole.TAG_EDITOR)).id},
                                headers=headers(admin))
    assert created.status_code == 201, created.text
    assert "lock" in calls and "grants" in calls and calls.index("lock") < calls.index("grants"), calls


async def _administrator_at_root(db, user) -> int:
    role = await builtin_role(db, BuiltinRole.ADMINISTRATOR)
    return (await db.exec(select(RoleAssignment.id).where(
        RoleAssignment.user_id == user.id, RoleAssignment.role_id == role.id,
        RoleAssignment.scope_key == ROOT.key))).one()


async def test_the_administrator_holds_both_base_roles(db):
    admin = await make_admin(db)
    assert await holds_admin_at_root(db, admin.id)
    only_system = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await assign(db, only_system, await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR), ROOT)
    assert not await holds_admin_at_root(db, only_system.id)
    assert (await authorization_report(db)).system_administrators == 1


async def test_the_only_administrator_keeps_administrator_at_the_root(client, db, headers):
    """Reproduced by the final review: the only system administrator removed their own administrator @ root (204)
    and nobody could administer users any more"""
    only = await make_admin(db)
    await give(db, only, ["post.view"])                    # another assignment: not the last one
    url = V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=only.id, assignment_id=await _administrator_at_root(db, only))
    problem(await client.delete(url, headers=headers(only)), 409, "cannot_remove_own_admin")

    manager = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    system = await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)
    administrator = await builtin_role(db, BuiltinRole.ADMINISTRATOR)
    from tests.helpers import _permission_names
    await give(db, manager, sorted(await _permission_names(db, system.id) | await _permission_names(db, administrator.id)))
    problem(await client.delete(url, headers=headers(manager)), 409, "last_admin_required")

    await make_admin(db)
    removed = await client.delete(url, headers=headers(manager))
    assert removed.status_code == 204, removed.text


async def test_a_grant_whose_role_kind_differs_from_its_table_grants_nothing(client, db, headers):
    user = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    tree_role = await make_role(db, ["database.view"], kind=RoleKind.TREE, allow_mixed=True)
    db.add(SystemRoleAssignment(user_id=user.id, role_id=tree_role.id))
    await db.commit()
    assert (await client.get(V1.path(DatabaseRoutes.STATUS), headers=headers(user))).status_code == 403
    report = await authorization_report(db)
    assert any(v.kind == "system" and v.role_id == tree_role.id and v.left_out == {"database.view"}
               for v in report.violations)
