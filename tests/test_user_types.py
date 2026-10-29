"""User types: permission levels, the type catalog (base and ceiling sets) and GET /user-types; the type change of
PATCH /users/{user_id} (spec section 4 and section 6 rules 2, 4 and 5), role edits bounded by the types of the
role's holders, and the user_type filter of the user list."""
from dataclasses import dataclass

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1 import users as users_routes
from app.backend.auth import assignments as assignment_rules
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.user_types import USER_TYPES, excess_permissions
from app.backend.bootstrap.permissions import PERMISSION_LEVELS, PERMISSION_META, permission_level, role_level
from app.backend.enums.general import PermissionLevel, Permissions, UserType
from app.backend.enums.routes import AuthRoutes, RoleRoutes, SearchRoutes, UserRoutes, UserTypeRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User, UserSession
from tests.helpers import assign, give, make_admin, make_group, make_post, make_role, make_user


def test_every_permission_declares_a_level():
    assert set(PERMISSION_LEVELS) == {p.value for p in Permissions}
    assert set(PERMISSION_META) == {p.value for p in Permissions}


def test_levels():
    assert permission_level("observation.view") == PermissionLevel.TREE
    assert permission_level("role.view") == PermissionLevel.TREE
    for name in ("role.create", "tag.archive", "media_format.update", "media.monitor",
                 "audit.view", "certificate.export"):
        assert permission_level(name) == PermissionLevel.SYSTEM, name
    # Units are managed below the root: a holder at a group manages the groups inside it
    for name in ("certificate.view", "certificate.create", "certificate.revoke",
                 "scope_group.create", "scope_group.update", "scope_group.archive"):
        assert permission_level(name) == PermissionLevel.TREE, name


def test_role_level():
    assert role_level(["observation.view"]) == PermissionLevel.TREE
    assert role_level([]) == PermissionLevel.TREE
    assert role_level(["observation.view", "tag.create"]) == PermissionLevel.SYSTEM


def test_boundary_contains_base_for_every_type():
    assert list(USER_TYPES) == list(UserType)
    for spec in USER_TYPES.values():
        assert spec.base_permissions <= spec.allowed_permissions


def test_system_administrator_holds_every_permission():
    spec = USER_TYPES[UserType.SYSTEM_ADMINISTRATOR]
    every = {p.value for p in Permissions}
    assert spec.base_permissions == every and spec.allowed_permissions == every


def test_operator_sets_match_spec():
    spec = USER_TYPES[UserType.OPERATOR]
    assert spec.base_permissions == {
        "post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view",
        "observation.create", "observation.update", "observation.archive", "media.create", "media.archive",
        "media_format.view",
    }
    assert spec.allowed_permissions == spec.base_permissions | {
        "observation.manage_any", "media.manage_any", "statistics.view", "export.create", "tag.create", "tag.update",
    }
    assert excess_permissions(UserType.OPERATOR, ["observation.view", "user.update"]) == {"user.update"}


def test_other_types_match_spec():
    view = {"post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view"}
    assert USER_TYPES[UserType.VIEWER].base_permissions == view
    assert USER_TYPES[UserType.VIEWER].allowed_permissions == view | {"statistics.view", "export.create"}
    assert USER_TYPES[UserType.ANALYST].base_permissions == view | {"statistics.view", "export.create"}
    assert USER_TYPES[UserType.ANALYST].allowed_permissions == (
        view | {"statistics.view", "export.create", "user.view", "scope_group.view"})
    operator = USER_TYPES[UserType.OPERATOR].base_permissions
    senior = USER_TYPES[UserType.SENIOR_OPERATOR]
    assert senior.base_permissions == operator | {
        "observation.manage_any", "media.manage_any", "statistics.view", "export.create"}
    assert senior.allowed_permissions == senior.base_permissions | {"media.update", "tag.create", "tag.update", "user.view"}
    administrator = USER_TYPES[UserType.ADMINISTRATOR]
    # the v2 post_admin base plus unit management (scope_group.create/update/archive) and the audit log of its node
    # (audit.view_scope): every tree permission
    assert administrator.base_permissions == senior.base_permissions | {
        "media.update", "post.create", "post.update", "post.archive", "user.view", "user.create", "user.update",
        "user.archive", "role.view", "scope_group.view", "certificate.view", "certificate.create",
        "certificate.revoke", "scope_group.create", "scope_group.update", "scope_group.archive",
        "audit.view_scope"}
    assert administrator.allowed_permissions == administrator.base_permissions | {
        "tag.create", "tag.update", "category.create", "category.update", "signal_type.create", "signal_type.update"}


async def test_user_types_catalog(client, db, headers):
    body = (await client.get(V1.path(UserTypeRoutes.ROOT), headers=headers(await make_user(db)))).json()
    assert [t["key"] for t in body] == [
        "viewer", "operator", "senior_operator", "analyst", "administrator", "system_administrator"]
    operator = body[1]
    assert [r["name"] for r in operator["base_roles"]] == ["operator"] and operator["base_roles"][0]["id"] > 0
    assert operator["tree_boundary"] == sorted(operator["tree_boundary"])
    assert operator["display_name"] == "Оператор посту"
    assert len(body[5]["tree_boundary"]) + len(body[5]["system_boundary"]) == len(Permissions)
    assert [r["name"] for r in body[5]["base_roles"]] == ["administrator", "system_administrator"]


async def test_user_types_need_authentication(client, db):
    response = await client.get(V1.path(UserTypeRoutes.ROOT))
    assert response.status_code == 401


# =================================================================== behaviour (spec section 4 and section 6 rule 4)
# Tree: East > Kharkiv > posts A, A2; West > post B.

@dataclass
class Tree:
    east: ScopeGroup
    kharkiv: ScopeGroup
    west: ScopeGroup
    a: Post
    a2: Post
    b: Post


@pytest.fixture
async def tree(db) -> Tree:
    east = await make_group(db, "East")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    west = await make_group(db, "West")
    return Tree(east, kharkiv, west, await make_post(db, "A", group_id=kharkiv.id),
                await make_post(db, "A2", group_id=kharkiv.id), await make_post(db, "B", group_id=west.id))


ME = V1.path(AuthRoutes.ME)
USERS = V1.path(UserRoutes.ROOT)
ROLE = V1.path(RoleRoutes.ROLE_BY_ID)
ROLE_PERMISSION = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID)


async def builtin(db, user_type: UserType) -> Role:
    """The tree base role of the type"""
    key = USER_TYPES[user_type].tree_base.role.value
    return (await db.exec(select(Role).where(Role.builtin_key == key))).one()


async def typed_user(db, user_type: UserType, post_id=None, node: Node = None) -> User:
    """A user of the type holding the built-in role of the type at node (default: the home post, the root without
    one)"""
    user = await make_user(db, post_id=post_id, user_type=user_type)
    if node is None:
        node = Node.post(post_id) if post_id is not None else ROOT
    await assign(db, user, await builtin(db, user_type), node)
    return user


async def permission_id(db, name: str) -> int:
    return (await db.exec(select(Permission.id).where(Permission.name == name))).one()


async def role_permissions(db, role: Role) -> set:
    rows = await db.exec(select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
                         .where(RolePermission.role_id == role.id))
    return set(rows.all())


async def assignment_rows(db, user: User) -> list:
    """(id, role id, scope key) of every assignment of the user, by id"""
    rows = await db.exec(select(RoleAssignment.id, RoleAssignment.role_id, RoleAssignment.scope_key)
                         .where(RoleAssignment.user_id == user.id).order_by(RoleAssignment.id))
    return [tuple(row) for row in rows.all()]


async def stored_type(db, user: User) -> str:
    return (await db.exec(select(User.user_type).where(User.id == user.id))).one()


async def change_type(client, login: dict, target: User, user_type: str):
    return await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), data={"user_type": user_type},
                              headers=login)


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


async def update_audit(db, user: User) -> AuditLog:
    return (await db.exec(select(AuditLog).where(AuditLog.resource_type == "user", AuditLog.action == "update",
                                                 AuditLog.resource_id == user.id))).one()


# ----------------------------------------------------------------- type change
async def test_type_change_swaps_base_role_and_ends_sessions(client, db, headers, tree):
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    (before,) = await assignment_rows(db, operator)
    old_login = headers(operator)
    assert (await client.get(ME, headers=old_login)).status_code == 200

    changed = await change_type(client, headers(admin), operator, "senior_operator")
    assert changed.status_code == 200, changed.text
    assert changed.json()["user_type"] == "senior_operator"
    senior = await builtin(db, UserType.SENIOR_OPERATOR)
    # The assignment keeps its id and its node; only the role changes
    assert await assignment_rows(db, operator) == [(before[0], senior.id, f"p:{tree.a.id}")]
    assert await stored_type(db, operator) == "senior_operator"

    refused = await client.get(ME, headers=old_login)
    assert refused.status_code == 401 and refused.json()["code"] == "session_expired"
    ended = (await db.exec(select(UserSession.end_reason, UserSession.revoke_cause)
                           .where(UserSession.user_id == operator.id))).all()
    assert [tuple(row) for row in ended] == [("revoked", "user_type_changed")]

    audit = await update_audit(db, operator)
    assert audit.old_values["user_type"] == "operator" and audit.new_values["user_type"] == "senior_operator"
    (replaced,) = audit.new_values["replaced_assignments"]
    assert replaced["before"]["id"] == replaced["after"]["id"] == before[0]
    assert (replaced["before"]["role_id"], replaced["after"]["role_id"]) == (before[1], senior.id)
    assert replaced["after"]["scope_key"] == f"p:{tree.a.id}"

    me = (await client.get(ME, headers=headers(operator))).json()
    assert me["user_type"] == "senior_operator"


async def test_downgrade_above_ceiling_refused(client, db, headers, tree):
    admin = await make_admin(db)
    senior = await typed_user(db, UserType.SENIOR_OPERATOR, post_id=tree.a.id)
    media_editor = await make_role(db, ["media.update"])
    extra = await assign(db, senior, media_editor, Node.post(tree.a.id))
    before = await assignment_rows(db, senior)
    login = headers(senior)

    body = problem(await change_type(client, headers(admin), senior, "operator"), 409, "user_type_ceiling_exceeded")
    assert body["user_type"] == "operator"
    assert body["assignments"][0]["excess_permissions"] == ["media.update"]
    assert body["assignments"] == [{
        "kind": "tree",
        "id": extra.id,
        "role": {"id": media_editor.id, "name": media_editor.name, "display_name": media_editor.display_name},
        "scope": {"kind": "post", "id": tree.a.id, "name": "A"},
        "excess_permissions": ["media.update"],
    }]
    # Nothing changed: type, assignments, sessions
    assert await stored_type(db, senior) == "senior_operator"
    assert await assignment_rows(db, senior) == before
    assert (await client.get(ME, headers=login)).status_code == 200
    assert not (await db.exec(select(AuditLog).where(AuditLog.resource_type == "user"))).all()


async def test_ceiling_counts_assignments_of_archived_roles(client, db, headers, tree):
    """An archived role grants nothing now, but its assignments apply again when it is restored"""
    admin = await make_admin(db)
    senior = await typed_user(db, UserType.SENIOR_OPERATOR, post_id=tree.a.id)
    media_editor = await make_role(db, ["media.update"])
    extra = await assign(db, senior, media_editor, Node.group(tree.kharkiv.id))
    media_editor.is_deleted = True
    db.add(media_editor)
    await db.commit()

    body = problem(await change_type(client, headers(admin), senior, "operator"), 409, "user_type_ceiling_exceeded")
    assert [(item["id"], item["scope"]) for item in body["assignments"]] == [
        (extra.id, {"kind": "group", "id": tree.kharkiv.id, "name": "Kharkiv"})]
    assert await stored_type(db, senior) == "senior_operator"


async def test_type_change_to_admin_puts_the_base_role_at_the_root(client, db, headers, tree):
    """The admin role holds system permissions: its assignment goes to the root, like the default assignment; two old
    base assignments become one (the second is merged into the first)"""
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    second = await assign(db, operator, await builtin(db, UserType.OPERATOR), Node.post(tree.b.id))
    (first, _) = await assignment_rows(db, operator)
    login = headers(operator)

    changed = await change_type(client, headers(admin), operator, "system_administrator")
    assert changed.status_code == 200, changed.text
    admin_role = await builtin(db, UserType.SYSTEM_ADMINISTRATOR)
    assert await assignment_rows(db, operator) == [(first[0], admin_role.id, "root")]
    assert (await client.get(ME, headers=login)).status_code == 401

    replaced = (await update_audit(db, operator)).new_values["replaced_assignments"]
    assert [item["before"]["id"] for item in replaced] == [first[0], second.id]
    assert replaced[0]["after"]["scope_key"] == "root" and replaced[0]["after"]["scope_group_id"] is None
    assert replaced[0]["after"]["scope_post_id"] is None
    # The merged row is removed: the audit keeps a full copy of it
    assert replaced[1]["merged_into"] == first[0] and "after" not in replaced[1]
    assert set(replaced[1]["before"]) == {"id", "user_id", "role_id", "scope_group_id", "scope_post_id", "scope_key",
                                          "created_at", "created_by"}
    assert replaced[1]["before"]["scope_key"] == f"p:{tree.b.id}"


async def test_type_change_merges_into_an_existing_assignment(client, db, headers, tree):
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    senior_role = await builtin(db, UserType.SENIOR_OPERATOR)
    kept = await assign(db, operator, senior_role, Node.post(tree.a.id))   # fits the operator ceiling

    changed = await change_type(client, headers(admin), operator, "senior_operator")
    assert changed.status_code == 200, changed.text
    assert await assignment_rows(db, operator) == [(kept.id, senior_role.id, f"p:{tree.a.id}")]
    (replaced,) = (await update_audit(db, operator)).new_values["replaced_assignments"]
    assert replaced["merged_into"] == kept.id


async def test_type_change_without_the_old_base_role(client, db, headers, tree):
    """Nothing to replace: the type changes, the ceiling is checked, the sessions end"""
    admin = await make_admin(db)
    user = await make_user(db, post_id=tree.a.id, user_type=UserType.OPERATOR)
    reader = await make_role(db, ["observation.view"])
    await assign(db, user, reader, Node.post(tree.a.id))
    before = await assignment_rows(db, user)
    login = headers(user)

    changed = await change_type(client, headers(admin), user, "viewer")
    assert changed.status_code == 200, changed.text
    assert await assignment_rows(db, user) == before
    assert await stored_type(db, user) == "viewer"
    assert (await client.get(ME, headers=login)).status_code == 401
    assert (await update_audit(db, user)).new_values["replaced_assignments"] == []


async def test_same_type_again_changes_nothing(client, db, headers, tree):
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    before = await assignment_rows(db, operator)
    login = headers(operator)

    changed = await change_type(client, headers(admin), operator, "operator")
    assert changed.status_code == 200, changed.text
    assert await assignment_rows(db, operator) == before
    assert (await client.get(ME, headers=login)).status_code == 200
    assert "replaced_assignments" not in (await update_audit(db, operator)).new_values


async def test_type_change_needs_the_new_base_role_at_the_node(client, db, headers, tree):
    """Rule 4: the caller could grant every assignment of the user (rule 2) and holds every permission of the new base
    role at the node of each replaced assignment"""
    await make_admin(db)
    east_admin = await typed_user(db, UserType.ADMINISTRATOR, node=Node.group(tree.east.id))
    west_admin = await typed_user(db, UserType.ADMINISTRATOR, node=Node.group(tree.west.id))
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    before = await assignment_rows(db, operator)

    # The operator lies outside the West administrator's user.view (audit L1)
    problem(await change_type(client, headers(west_admin), operator, "senior_operator"), 403, "user_self_only")
    body = problem(await change_type(client, headers(east_admin), operator, "system_administrator"), 403,
                   "grant_exceeds_own_permissions")
    assert "user.create" in body["missing"]          # administrator at the root, beyond East
    assert await assignment_rows(db, operator) == before and await stored_type(db, operator) == "operator"

    changed = await change_type(client, headers(east_admin), operator, "senior_operator")
    assert changed.status_code == 200, changed.text
    senior_role = await builtin(db, UserType.SENIOR_OPERATOR)
    assert await assignment_rows(db, operator) == [(before[0][0], senior_role.id, f"p:{tree.a.id}")]


async def test_type_change_needs_the_new_base_role_where_a_new_user_would_get_it(client, db, headers, tree):
    """R29: with nothing to replace, the caller still needs the new base role at the node of the default assignment
    (the home post; the root without one or for a system role), the bar of POST /users/"""
    await make_admin(db)
    east_admin = await typed_user(db, UserType.ADMINISTRATOR, node=Node.group(tree.east.id))
    user = await make_user(db, post_id=tree.a.id, user_type=UserType.OPERATOR)
    await assign(db, user, await make_role(db, ["observation.view"]), Node.post(tree.a.id))
    before = await assignment_rows(db, user)
    login = headers(user)

    body = problem(await change_type(client, headers(east_admin), user, "system_administrator"), 403, "grant_exceeds_own_permissions")
    assert "user.create" in body["missing"]
    assert await stored_type(db, user) == "operator" and await assignment_rows(db, user) == before
    assert (await client.get(ME, headers=login)).status_code == 200

    # Themselves: a delegated administrator holding a custom copy of the post_admin role cannot become an admin
    copy = await make_role(db, USER_TYPES[UserType.ADMINISTRATOR].base_permissions)
    delegate = await make_user(db, post_id=tree.a.id, user_type=UserType.ADMINISTRATOR)
    await assign(db, delegate, copy, Node.group(tree.east.id))
    problem(await change_type(client, headers(delegate), delegate, "system_administrator"), 403, "grant_exceeds_own_permissions")
    assert await stored_type(db, delegate) == "administrator"

    # Inside the caller's reach: allowed, the assignments stay
    changed = await change_type(client, headers(east_admin), user, "senior_operator")
    assert changed.status_code == 200, changed.text
    assert await assignment_rows(db, user) == before and await stored_type(db, user) == "senior_operator"


async def test_type_change_and_deactivation_in_one_request(client, db, headers, tree):
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    (before,) = await assignment_rows(db, operator)
    login = headers(operator)

    changed = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=operator.id), headers=headers(admin),
                                 data={"user_type": "senior_operator", "is_active": "false"})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["user_type"], changed.json()["is_active"]) == ("senior_operator", False)
    senior = await builtin(db, UserType.SENIOR_OPERATOR)
    assert await assignment_rows(db, operator) == [(before[0], senior.id, f"p:{tree.a.id}")]
    ended = (await db.exec(select(UserSession.end_reason, UserSession.revoke_cause)
                           .where(UserSession.user_id == operator.id))).all()
    assert [tuple(row) for row in ended] == [("revoked", "account_deactivated")]   # the deactivation names the cause
    assert len((await update_audit(db, operator)).new_values["replaced_assignments"]) == 1
    assert (await client.get(ME, headers=login)).status_code == 401


async def test_a_concurrent_base_role_assignment_is_refused_by_the_unique_index(client, db, headers, tree,
                                                                                monkeypatch):
    """R30: another request assigns the new base role at the same node after the type change read the user's
    assignments: the unique index answers assignment_exists, nothing written"""
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    await assign(db, operator, await builtin(db, UserType.SENIOR_OPERATOR), Node.post(tree.a.id))
    before = await assignment_rows(db, operator)
    login = headers(operator)

    async def not_yet_committed(*args, **kwargs) -> dict:
        return {}
    monkeypatch.setattr(assignment_rules, "_held_nodes", not_yet_committed, raising=False)
    problem(await change_type(client, headers(admin), operator, "senior_operator"), 409, "assignment_exists")
    assert await assignment_rows(db, operator) == before and await stored_type(db, operator) == "operator"
    assert (await client.get(ME, headers=login)).status_code == 200
    assert not (await db.exec(select(AuditLog).where(AuditLog.resource_type == "user"))).all()


async def test_a_username_race_with_a_type_change_answers_username_taken(client, db, headers, tree, monkeypatch):
    """R30: the type change flushes the pending username: the unique index answers username_taken, not a 500"""
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    other = await make_user(db, post_id=tree.a.id)
    monkeypatch.setattr(users_routes, "_username_taken", lambda username, exclude_user_id=None: select(User).where(User.id == -1))

    refused = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=operator.id), headers=headers(admin),
                                 data={"username": other.username, "user_type": "senior_operator"})
    problem(refused, 400, "username_taken")
    assert await stored_type(db, operator) == "operator"


async def test_the_audit_row_is_written_with_the_change(client, db, headers, tree, monkeypatch):
    """R30: the type change and its audit row are one transaction: when the audit row cannot be written, the
    assignments and the type stay as they were"""
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    before = await assignment_rows(db, operator)

    async def broken(*args, **kwargs):
        raise RuntimeError("audit storage unavailable")
    monkeypatch.setattr(users_routes, "create_audit_log", broken)
    failed = await change_type(client, headers(admin), operator, "system_administrator")
    assert failed.status_code == 500 and failed.json()["code"] == "internal_error"
    assert await assignment_rows(db, operator) == before and await stored_type(db, operator) == "operator"


# ----------------------------------------------------------------- the last administrator (rule 5)
async def test_last_admin_cannot_downgrade_self(client, db, headers):
    admin = await make_admin(db)
    before = await assignment_rows(db, admin)
    login = headers(admin)

    problem(await change_type(client, login, admin, "operator"), 409, "cannot_remove_own_admin")
    assert await stored_type(db, admin) == "system_administrator"
    assert await assignment_rows(db, admin) == before
    assert (await client.get(ME, headers=login)).status_code == 200


async def test_admin_cannot_downgrade_self_while_another_admin_remains(client, db, headers):
    admin = await make_admin(db)
    peer = await make_admin(db)
    problem(await change_type(client, headers(admin), admin, "analyst"), 409, "cannot_remove_own_admin")

    changed = await change_type(client, headers(admin), peer, "analyst")
    assert changed.status_code == 200, changed.text
    analyst = await builtin(db, UserType.ANALYST)
    assert [row[1:] for row in await assignment_rows(db, peer)] == [(analyst.id, "root")]


async def test_the_only_admin_cannot_be_downgraded_by_another_caller(client, db, headers):
    """A caller holding every permission through a custom role is not the administrator: downgrading the only holder of
    the built-in admin role at the root leaves no administrator"""
    only = await make_admin(db)
    manager = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await give(db, manager, [p.value for p in Permissions])

    problem(await change_type(client, headers(manager), only, "analyst"), 409, "last_admin_required")
    assert await stored_type(db, only) == "system_administrator"
    admin_role = await builtin(db, UserType.SYSTEM_ADMINISTRATOR)
    assert [row[1:] for row in await assignment_rows(db, only)] == [(admin_role.id, "root")]


# ----------------------------------------------------------------- role edits bounded by the holders
async def test_role_update_refused_above_holder_ceiling(client, db, headers, tree):
    admin = await make_admin(db)
    viewer = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    reader = await make_role(db, ["observation.view"])
    await assign(db, viewer, reader, Node.post(tree.a.id))
    view_id, update_id = await permission_id(db, "observation.view"), await permission_id(db, "observation.update")
    expected = [{"id": viewer.id, "username": viewer.username, "user_type": "viewer",
                 "excess_permissions": ["observation.update"]}]

    patched = await client.patch(ROLE.format(role_id=reader.id), headers=headers(admin),
                                 json={"display_name": "Changed", "permission_ids": [view_id, update_id]})
    assert problem(patched, 409, "role_holders_exceed_user_type")["users"] == expected
    put = await client.put(ROLE_PERMISSION.format(role_id=reader.id, permission_id=update_id), headers=headers(admin))
    assert problem(put, 409, "role_holders_exceed_user_type")["users"] == expected
    # The role is unchanged
    assert await role_permissions(db, reader) == {"observation.view"}
    assert (await db.exec(select(Role.display_name).where(Role.id == reader.id))).one() == reader.display_name

    # Within the ceiling of every holder
    stats_id = await permission_id(db, "statistics.view")
    allowed = await client.put(ROLE_PERMISSION.format(role_id=reader.id, permission_id=stats_id), headers=headers(admin))
    assert allowed.status_code == 200, allowed.text
    assert await role_permissions(db, reader) == {"observation.view", "statistics.view"}


async def test_archived_holders_bound_role_edits(client, db, headers, tree):
    """R28: an archived user gets the role back with the restore (POST /users/{user_id}/restore re-checks nothing), so
    they count as a holder for the ceiling and for the root level, and are listed"""
    admin = await make_admin(db)
    viewer = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    tagger = await make_role(db, ["tag.view"])
    await assign(db, viewer, tagger, Node.group(tree.east.id))
    viewer.is_deleted, viewer.is_active = True, False
    db.add(viewer)
    await db.commit()
    tag_view, update_id = await permission_id(db, "tag.view"), await permission_id(db, "observation.update")
    tag_create = await permission_id(db, "tag.create")
    expected = [{"id": viewer.id, "username": viewer.username, "user_type": "viewer",
                 "excess_permissions": ["observation.update"]}]

    patched = await client.patch(ROLE.format(role_id=tagger.id), headers=headers(admin),
                                 json={"permission_ids": [tag_view, update_id]})
    assert problem(patched, 409, "role_holders_exceed_user_type")["users"] == expected
    put = await client.put(ROLE_PERMISSION.format(role_id=tagger.id, permission_id=update_id), headers=headers(admin))
    assert problem(put, 409, "role_holders_exceed_user_type")["users"] == expected

    # A system permission never enters a tree role, whoever holds it
    patched = await client.patch(ROLE.format(role_id=tagger.id), headers=headers(admin),
                                 json={"permission_ids": [tag_view, tag_create]})
    assert problem(patched, 409, "permission_level_mismatch")["permissions"] == ["tag.create"]
    put = await client.put(ROLE_PERMISSION.format(role_id=tagger.id, permission_id=tag_create), headers=headers(admin))
    assert problem(put, 409, "permission_level_mismatch")["permissions"] == ["tag.create"]
    assert await role_permissions(db, tagger) == {"tag.view"}


async def test_a_holder_with_several_assignments_is_listed_once(client, db, headers, tree):
    """Only the permissions a holder's type does not allow are listed, once per holder whatever their assignments"""
    admin = await make_admin(db)
    viewer = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    reader = await make_role(db, ["observation.view"])
    for node in (Node.post(tree.a.id), Node.post(tree.b.id), Node.group(tree.east.id)):
        await assign(db, viewer, reader, node)
    await assign(db, operator, reader, Node.post(tree.a.id))
    ids = [await permission_id(db, name) for name in ("observation.view", "observation.update", "statistics.view")]

    patched = await client.patch(ROLE.format(role_id=reader.id), headers=headers(admin), json={"permission_ids": ids})
    assert problem(patched, 409, "role_holders_exceed_user_type")["users"] == [
        {"id": viewer.id, "username": viewer.username, "user_type": "viewer",
         "excess_permissions": ["observation.update"]}]
    assert await role_permissions(db, reader) == {"observation.view"}


async def test_role_edit_refused_with_a_permission_of_another_level(client, db, headers, tree):
    admin = await make_admin(db)
    group_admin = await typed_user(db, UserType.ADMINISTRATOR, node=Node.group(tree.east.id))
    tagger = await make_role(db, ["tag.view"])
    await assign(db, group_admin, tagger, Node.group(tree.east.id))
    view_id, create_id = await permission_id(db, "tag.view"), await permission_id(db, "tag.create")

    patched = await client.patch(ROLE.format(role_id=tagger.id), headers=headers(admin),
                                 json={"permission_ids": [view_id, create_id]})
    assert problem(patched, 409, "permission_level_mismatch")["role_kind"] == "tree"
    put = await client.put(ROLE_PERMISSION.format(role_id=tagger.id, permission_id=create_id), headers=headers(admin))
    assert problem(put, 409, "permission_level_mismatch")["permissions"] == ["tag.create"]
    assert await role_permissions(db, tagger) == {"tag.view"}

    # A system role held by a user whose type allows the permission gets it
    root_holder = await typed_user(db, UserType.ADMINISTRATOR)
    (editors,) = await give(db, root_holder, ["tag.update"])
    allowed = await client.put(ROLE_PERMISSION.format(role_id=editors.id, permission_id=create_id), headers=headers(admin))
    assert allowed.status_code == 200, allowed.text


# ----------------------------------------------------------------- role edits bounded by the editor's nodes (R36)
async def role_editor(db, tree, *root_perms) -> User:
    """Type system_administrator, holding custom roles with the root_perms (default role.view + role.update: a tree
    role with role.view at the root and a system role with the rest) and {observation.view, observation.update} at
    post A only (a converted custom administrator)"""
    editor = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await give(db, editor, root_perms or ["role.view", "role.update"])
    await assign(db, editor, await make_role(db, ["observation.view", "observation.update"]), Node.post(tree.a.id))
    return editor


async def test_added_permissions_need_the_editor_at_every_node_of_the_role(client, db, headers, tree):
    """R36: a permission added to a role applies at every node the role is assigned at, so the editor must hold it
    there: holding it at post A does not let it reach the root (the editor's own role) or West (another user's)"""
    admin = await make_admin(db)
    editor = await role_editor(db, tree)
    login = headers(editor)
    own_role = (await db.exec(select(RoleAssignment.role_id).where(RoleAssignment.user_id == editor.id,
                                                                    RoleAssignment.scope_key == ROOT.key))).one()
    own = (await db.exec(select(Role).where(Role.id == own_role))).one()
    operator = await make_user(db, post_id=tree.b.id, user_type=UserType.OPERATOR)
    at_west = await make_role(db, ["observation.view"])
    await assign(db, operator, at_west, Node.group(tree.west.id))
    view_id, update_id = await permission_id(db, "observation.view"), await permission_id(db, "observation.update")
    role_ids = [await permission_id(db, "role.view")]

    for role, kept in ((own, {"role.view"}), (at_west, {"observation.view"})):
        put = await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=update_id), headers=login)
        assert problem(put, 403, "grant_exceeds_own_permissions")["missing"] == ["observation.update"]
        wanted = [*role_ids, update_id] if role is own else [view_id, update_id]
        patched = await client.patch(ROLE.format(role_id=role.id), headers=login,
                                     json={"display_name": "Changed", "permission_ids": wanted})
        assert problem(patched, 403, "grant_exceeds_own_permissions")["missing"] == ["observation.update"]
        assert await role_permissions(db, role) == kept
        assert (await db.exec(select(Role.display_name).where(Role.id == role.id))).one() == role.display_name
    me = (await client.get(ME, headers=login)).json()
    assert "observation.update" not in me["root_permissions"]

    # A role assigned only where the editor holds the permission (post A) gets it
    at_a = await make_role(db, ["observation.view"])
    await assign(db, operator, at_a, Node.post(tree.a.id))
    allowed = await client.put(ROLE_PERMISSION.format(role_id=at_a.id, permission_id=update_id), headers=login)
    assert allowed.status_code == 200, allowed.text
    # A full administrator (the admin role at the root) edits any role
    allowed = await client.put(ROLE_PERMISSION.format(role_id=at_west.id, permission_id=update_id), headers=headers(admin))
    assert allowed.status_code == 200, allowed.text
    assert await role_permissions(db, at_west) == {"observation.view", "observation.update"}


async def test_added_permissions_count_assignments_of_archived_users(client, db, headers, tree):
    """R36 with R28: an archived user's assignment comes back with the restore, so its node counts too"""
    editor = await role_editor(db, tree)
    holder = await make_user(db, post_id=tree.b.id, user_type=UserType.OPERATOR)
    role = await make_role(db, ["observation.view"])
    await assign(db, holder, role, Node.post(tree.b.id))
    holder.is_deleted, holder.is_active = True, False
    db.add(holder)
    await db.commit()

    put = await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=await permission_id(db, "observation.update")),
                           headers=headers(editor))
    assert problem(put, 403, "grant_exceeds_own_permissions")["missing"] == ["observation.update"]
    assert await role_permissions(db, role) == {"observation.view"}


async def test_role_restore_needs_its_permissions_at_every_node_of_the_role(client, db, headers, tree):
    """R36: a restore gives the role's permissions back at every node it is assigned at"""
    admin = await make_admin(db)
    restorer = await role_editor(db, tree, "role.view", "role.archive")
    holder = await make_user(db, post_id=tree.b.id, user_type=UserType.OPERATOR)
    at_b, at_a, unassigned = [await make_role(db, ["observation.view"]) for _ in range(3)]
    await assign(db, holder, at_b, Node.post(tree.b.id))
    await assign(db, holder, at_a, Node.post(tree.a.id))
    for role in (at_b, at_a, unassigned):
        role.is_deleted = True
        db.add(role)
    await db.commit()
    restore = V1.path(RoleRoutes.ROLE_RESTORE)

    refused = await client.post(restore.format(role_id=at_b.id), headers=headers(restorer))
    assert problem(refused, 403, "grant_exceeds_own_permissions")["missing"] == ["observation.view"]
    assert (await db.exec(select(Role.is_deleted).where(Role.id == at_b.id))).one() is True

    # Held at the node of every assignment (post A), or held anywhere for a role assigned nowhere
    for role in (at_a, unassigned):
        restored = await client.post(restore.format(role_id=role.id), headers=headers(restorer))
        assert restored.status_code == 200, restored.text
    restored = await client.post(restore.format(role_id=at_b.id), headers=headers(admin))
    assert restored.status_code == 200, restored.text


# ----------------------------------------------------------------- the user list
async def test_user_list_filters_by_type(client, db, headers, tree):
    admin = await make_admin(db)
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    await typed_user(db, UserType.VIEWER, post_id=tree.a.id)

    listed = await client.get(USERS, params={"user_type": "operator"}, headers=headers(admin))
    assert listed.status_code == 200, listed.text
    assert [(user["id"], user["user_type"]) for user in listed.json()] == [(operator.id, "operator")]
    admins = (await client.get(USERS, params={"user_type": "system_administrator"}, headers=headers(admin))).json()
    assert [user["id"] for user in admins] == [admin.id]

    searched = await client.get(V1.path(SearchRoutes.USERS), params={"user_type": "operator"}, headers=headers(admin))
    assert searched.status_code == 200, searched.text
    assert [user["id"] for user in searched.json()["items"]] == [operator.id] and searched.json()["total"] == 1

    unknown = await client.get(USERS, params={"user_type": "boss"}, headers=headers(admin))
    assert unknown.status_code == 422
    one = await client.get(V1.path(UserRoutes.USER_BY_ID, user_id=operator.id), headers=headers(admin))
    assert one.json()["user_type"] == "operator"


def test_user_type_is_required_in_the_user_schema():
    from app.main import fastapi_app
    schemas = V1.openapi(fastapi_app)["components"]["schemas"]
    assert "user_type" in schemas["UserResponse"]["required"]


def test_user_type_params_of_problems_are_the_enum():
    """openapi-typescript narrows them to the type keys"""
    from app.main import fastapi_app
    schemas = V1.openapi(fastapi_app)["components"]["schemas"]
    for name in ("RoleExceedsUserTypeProblem", "UserTypeCeilingExceededProblem", "ExcessHolder"):
        assert schemas[name]["properties"]["user_type"]["$ref"].endswith("/UserType"), name
