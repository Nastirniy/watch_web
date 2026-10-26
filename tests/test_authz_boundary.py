"""Authorization v3: the boundary at evaluation. Each test writes rows only a bug (or hand-written SQL) could write,
and checks that they never open access: a permission puts in effect only what its level and the boundary of the
user's type allow (auth/integrity.py)."""
from loguru import logger

from app.backend.api.v1 import V1
from app.backend.auth import context
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import BuiltinRole, RoleKind, UserType
from app.backend.enums.routes import AuthRoutes, DatabaseRoutes, UserRoutes
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.user import User
from tests.helpers import assign, builtin_role, give, make_post, make_role, make_user

ME = V1.path(AuthRoutes.ME)
CHECK = V1.path(AuthRoutes.CHECK_PERMISSION)
STATUS = V1.path(DatabaseRoutes.STATUS)


async def allowed(client, login: dict, names: list, post_id=None) -> bool:
    body = {"all": names} if post_id is None else {"all": names, "post_id": post_id}
    response = await client.post(CHECK, json=body, headers=login)
    assert response.status_code == 200, response.text
    return response.json()["allowed"]


async def test_system_permission_in_tree_role_below_root_not_in_effect(client, db, headers):
    post = await make_post(db)
    user = await make_user(db, post_id=post.id, user_type=UserType.SYSTEM_ADMINISTRATOR)
    broken = await make_role(db, ["post.view", "database.view"], kind=RoleKind.TREE, allow_mixed=True)
    await assign(db, user, broken, Node.post(post.id))
    login = headers(user)

    denied = await client.get(STATUS, headers=login)
    assert denied.status_code == 403 and denied.json()["missing"] == ["database.view"]
    me = (await client.get(ME, headers=login)).json()
    assert "post.view" in me["permissions"] and "database.view" not in me["permissions"]


async def test_role_outside_type_boundary_not_in_effect(client, db, headers):
    post = await make_post(db)
    operator = await make_user(db, post_id=post.id, user_type=UserType.OPERATOR)
    await assign(db, operator, await builtin_role(db, BuiltinRole.OPERATOR), Node.post(post.id))
    await assign(db, operator, await make_role(db, ["user.view", "user.create"]), Node.post(post.id))
    login = headers(operator)

    assert not await allowed(client, login, ["user.create"], post.id)
    assert await allowed(client, login, ["observation.create"], post.id)
    assert "user.create" not in (await client.get(ME, headers=login)).json()["permissions"]


async def test_system_role_in_tree_table_grants_nothing(client, db, headers):
    user = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    system_administrator = await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)
    db.add(RoleAssignment(user_id=user.id, role_id=system_administrator.id, **scope_columns(ROOT)))
    await db.commit()
    assert (await client.get(STATUS, headers=headers(user))).status_code == 403


async def test_boundary_applies_from_the_next_request(client, db, headers):
    """Review Focus 1: the boundary is read at every request; nothing is cached with the login"""
    post = await make_post(db)
    senior = await make_user(db, post_id=post.id, user_type=UserType.SENIOR_OPERATOR)
    await assign(db, senior, await make_role(db, ["media.view", "media.update"]), Node.post(post.id))
    login = headers(senior)
    assert await allowed(client, login, ["media.update"], post.id)

    # a narrower boundary for this user (a release that moved the user's type, a hand edit): the same token
    await db.execute(User.__table__.update().where(User.id == senior.id).values(user_type=UserType.OPERATOR.value))
    await db.commit()
    assert not await allowed(client, login, ["media.update"], post.id)
    assert await allowed(client, login, ["media.view"], post.id)


async def test_archived_system_role_grants_nothing_and_restore_revives(client, db, headers):
    """Review Focus 2"""
    user = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    (status_reader,) = await give(db, user, ["database.view"])
    login = headers(user)
    assert (await client.get(STATUS, headers=login)).status_code == 200

    for archived, expected in ((True, 403), (False, 200)):
        await db.execute(Role.__table__.update().where(Role.id == status_reader.id).values(is_deleted=archived))
        await db.commit()
        assert (await client.get(STATUS, headers=login)).status_code == expected


async def test_warning_logged_once_per_grant(client, db, headers):
    context._WARNED.clear()     # once per process: ids repeat between the tests' databases
    post = await make_post(db)
    user = await make_user(db, post_id=post.id, user_type=UserType.VIEWER)
    broken = await make_role(db, ["post.view", "user.archive"])
    assignment = await assign(db, user, broken, Node.post(post.id))
    # a first request configures the application's logging (it replaces the sinks): add ours after it
    await client.get(ME, headers=headers(await make_user(db, post_id=post.id, user_type=UserType.VIEWER)))
    lines = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        login = headers(user)
        await client.get(ME, headers=login)
        await client.get(ME, headers=login)
    finally:
        logger.remove(sink)
    matching = [line for line in lines if f"tree:{assignment.id}" in line and "not in effect" in line]
    assert len(matching) == 1, (lines, assignment.id)
    assert "user.archive" in matching[0] and user.username not in matching[0]


async def test_delegation_reads_effective_permissions(client, db, headers):
    """A grant the boundary leaves out does not make its holder more privileged than the caller"""
    post = await make_post(db)
    target = await make_user(db, post_id=post.id, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), Node.post(post.id))
    await assign(db, target, await make_role(db, ["user.view", "user.create"]), Node.post(post.id))
    caller = await make_user(db, post_id=post.id, user_type=UserType.ADMINISTRATOR)
    await assign(db, caller, await builtin_role(db, BuiltinRole.OPERATOR), Node.post(post.id))
    await assign(db, caller, await make_role(db, ["user.view", "user.update"]), Node.post(post.id))

    response = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), data={"username_alias": "Renamed"},
                                  headers=headers(caller))
    assert response.status_code == 200, response.text
