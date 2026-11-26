"""Role assignments through the API (authorization v2, spec sections 6 and 7): GET and POST
/users/{user_id}/assignments, DELETE /users/{user_id}/assignments/{assignment_id}.

Delegation at scope (the caller holds the role's permissions at the node and user.update covering the user, who has
no assignment the caller could not grant), the type ceiling, system roles only at the root, live nodes only, at
least one assignment per user and the last administrator. Scope and ownership answer before the existence or the
state of a group or a post, so an outsider learns nothing about ids.

Tree of every test: East > Kharkiv > post A; West > post B. The group admin is of type administrator with the built-in
administrator role at East.
"""
from dataclasses import dataclass

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth import assignments as assignment_rules
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.user_types import USER_TYPES
from app.backend.config import settings
from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import ObservationRoutes, PostRoutes, UserRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User
from tests.helpers import (
    assign, make_admin, make_dictionary, make_group, make_post, make_role, make_user, observation_payload,
)

ASSIGNMENT_COLUMNS = {"id", "user_id", "role_id", "scope_group_id", "scope_post_id", "scope_key", "created_at",
                      "created_by"}


@dataclass
class Tree:
    east: ScopeGroup
    kharkiv: ScopeGroup
    west: ScopeGroup
    a: Post
    b: Post


@pytest.fixture
async def tree(db) -> Tree:
    east = await make_group(db, "East")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    west = await make_group(db, "West")
    return Tree(east, kharkiv, west, await make_post(db, "A", group_id=kharkiv.id), await make_post(db, "B", group_id=west.id))


async def builtin(db, user_type: UserType) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == user_type.value))).one()


async def typed_user(db, user_type: UserType, post_id=None, node: Node = None) -> User:
    """A user of the type holding the built-in role of the type at node (default: the home post, the root without
    one)"""
    user = await make_user(db, post_id=post_id, user_type=user_type)
    if node is None:
        node = Node.post(post_id) if post_id is not None else ROOT
    await assign(db, user, await builtin(db, user_type), node)
    return user


async def group_admin_of(db, group: ScopeGroup) -> User:
    return await typed_user(db, UserType.ADMINISTRATOR, node=Node.group(group.id))


async def _archive(db, row) -> None:
    row.is_deleted = True
    db.add(row)
    await db.commit()


def assignments_url(user_id: int) -> str:
    return V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=user_id)


def assignment_url(user_id: int, assignment_id: int) -> str:
    return V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=user_id, assignment_id=assignment_id)


def scope(kind: str, node_id: int = None) -> dict:
    return {"kind": kind} if node_id is None else {"kind": kind, "id": node_id}


def body_of(role: Role, kind: str, node_id: int = None) -> dict:
    return {"role_id": role.id, "scope": scope(kind, node_id)}


async def assignment_ids(db, user: User) -> list:
    rows = await db.exec(select(RoleAssignment.id).where(RoleAssignment.user_id == user.id).order_by(RoleAssignment.id))
    return list(rows.all())


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


# ----------------------------------------------------------------- delegation at scope
async def test_group_admin_assigns_inside_own_subtree(client, db, headers, tree):
    group_admin = await group_admin_of(db, tree.east)
    target = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    operator = await builtin(db, UserType.OPERATOR)

    created = await client.post(assignments_url(target.id), json=body_of(operator, "group", tree.kharkiv.id),
                                headers=headers(group_admin))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["role"] == {"id": operator.id, "name": operator.name, "display_name": operator.display_name,
                            "is_builtin": True, "is_deleted": False}
    assert body["scope"] == {"kind": "group", "id": tree.kharkiv.id, "name": "Kharkiv"}
    assert body["created_at"]

    listed = await client.get(assignments_url(target.id), headers=headers(group_admin))
    assert listed.status_code == 200, listed.text
    assert [(item["scope"]["kind"], item["scope"]["id"], item["scope"]["name"]) for item in listed.json()] == [
        ("post", tree.a.id, "A"), ("group", tree.kharkiv.id, "Kharkiv"),
    ]
    assert listed.json()[1] == body

    row = (await db.exec(select(RoleAssignment).where(RoleAssignment.id == body["id"]))).one()
    assert (row.role_id, row.scope_key, row.scope_group_id, row.scope_post_id, row.created_by) == (
        operator.id, f"g:{tree.kharkiv.id}", tree.kharkiv.id, None, group_admin.id)
    audit = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role_assignment"))).one()
    assert (audit.action, audit.resource_id, audit.user_id) == ("create", body["id"], group_admin.id)
    assert set(audit.new_values) == ASSIGNMENT_COLUMNS and audit.new_values["user_id"] == target.id


async def test_group_admin_cannot_assign_outside_own_subtree(client, db, headers, tree):
    """Review Focus 2: a delegated administrator at East assigning at the root, at West, or to a user whose home post
    is outside East: 403, nothing written"""
    group_admin = await group_admin_of(db, tree.east)
    target = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    outsider = await typed_user(db, UserType.OPERATOR, post_id=tree.b.id)
    operator = await builtin(db, UserType.OPERATOR)
    before = {user.id: await assignment_ids(db, user) for user in (target, outsider)}

    for kind, node_id in (("root", None), ("group", tree.west.id), ("post", tree.b.id)):
        refused = await client.post(assignments_url(target.id), json=body_of(operator, kind, node_id),
                                    headers=headers(group_admin))
        body = problem(refused, 403, "grant_exceeds_own_permissions")
        assert body["missing"] == sorted(USER_TYPES[UserType.OPERATOR].base_permissions | {"user.update"}), kind

    # The outsider lies outside the group admin's user.view: the same 403 as GET /users/{id} (audit L1)
    refused = await client.post(assignments_url(outsider.id), json=body_of(operator, "group", tree.kharkiv.id),
                                headers=headers(group_admin))
    problem(refused, 403, "user_self_only")
    refused = await client.delete(assignment_url(outsider.id, before[outsider.id][0]), headers=headers(group_admin))
    problem(refused, 403, "user_self_only")

    # A user of A who also holds a role at West: the group admin could not grant it, so cannot change the user
    await assign(db, target, await builtin(db, UserType.VIEWER), Node.group(tree.west.id))
    before[target.id] = await assignment_ids(db, target)
    refused = await client.post(assignments_url(target.id), json=body_of(operator, "group", tree.kharkiv.id),
                                headers=headers(group_admin))
    problem(refused, 403, "target_user_more_privileged")

    for user in (target, outsider):
        assert await assignment_ids(db, user) == before[user.id]
    assert not (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role_assignment"))).all()


async def test_group_admin_removes_inside_own_subtree(client, db, headers, tree):
    group_admin = await group_admin_of(db, tree.east)
    target = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    extra = await assign(db, target, await builtin(db, UserType.VIEWER), Node.group(tree.kharkiv.id))

    removed = await client.delete(assignment_url(target.id, extra.id), headers=headers(group_admin))
    assert removed.status_code == 204, removed.text
    assert len(await assignment_ids(db, target)) == 1
    audit = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role_assignment"))).one()
    assert (audit.action, audit.resource_id, audit.user_id) == ("delete", extra.id, group_admin.id)
    # A full copy of the removed row: every column
    assert set(audit.old_values) == ASSIGNMENT_COLUMNS
    assert audit.old_values["scope_key"] == f"g:{tree.kharkiv.id}" and audit.old_values["user_id"] == target.id


async def test_group_admin_cannot_touch_users_without_home_post(client, db, headers, tree):
    """Review Focus 4: an analyst at the root without a home post is outside every group admin's reach"""
    group_admin = await group_admin_of(db, tree.east)
    analyst = await typed_user(db, UserType.ANALYST)
    (analyst_assignment,) = await assignment_ids(db, analyst)
    viewer = await builtin(db, UserType.VIEWER)

    problem(await client.get(V1.path(UserRoutes.USER_BY_ID, user_id=analyst.id), headers=headers(group_admin)),
            403, "user_self_only")
    problem(await client.get(assignments_url(analyst.id), headers=headers(group_admin)), 403, "user_self_only")
    # Writes answer like the reads: the analyst lies outside the group admin's user.view (audit L1)
    refused = await client.post(assignments_url(analyst.id), json=body_of(viewer, "group", tree.kharkiv.id),
                                headers=headers(group_admin))
    problem(refused, 403, "user_self_only")
    refused = await client.delete(assignment_url(analyst.id, analyst_assignment), headers=headers(group_admin))
    problem(refused, 403, "user_self_only")
    assert await assignment_ids(db, analyst) == [analyst_assignment]


async def test_scoped_caller_gets_the_same_403_for_unknown_archived_and_foreign_nodes(client, db, headers, tree):
    """R19: an unknown id, an archived node outside the caller's scope and a live one outside it answer alike;
    404 answers only inside the caller's scope"""
    group_admin = await group_admin_of(db, tree.east)
    target = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    operator = await builtin(db, UserType.OPERATOR)
    gone_group = await make_group(db, "Gone", parent_id=tree.west.id)
    await _archive(db, gone_group)
    gone_post = await make_post(db, "Gone post", group_id=tree.west.id)
    await _archive(db, gone_post)

    for kind, ids in (("group", (999999, gone_group.id, tree.west.id)), ("post", (999999, gone_post.id, tree.b.id))):
        answers = []
        for node_id in ids:
            refused = await client.post(assignments_url(target.id), json=body_of(operator, kind, node_id),
                                        headers=headers(group_admin))
            body = problem(refused, 403, "grant_exceeds_own_permissions")
            answers.append((body["missing"], body.get("required")))
        assert answers[0] == answers[1] == answers[2], kind

    inside = await make_group(db, "Inside", parent_id=tree.east.id)
    await _archive(db, inside)
    problem(await client.post(assignments_url(target.id), json=body_of(operator, "group", inside.id),
                              headers=headers(group_admin)), 404, "scope_group_not_found")
    gone_a = await make_post(db, "Gone A", group_id=tree.kharkiv.id)
    await _archive(db, gone_a)
    body = problem(await client.post(assignments_url(target.id), json=body_of(operator, "post", gone_a.id),
                                     headers=headers(group_admin)), 404, "post_not_found")
    assert body["post_id"] == gone_a.id


async def test_a_role_without_permissions_needs_user_update_at_the_node(client, db, headers, tree):
    """R22: an assignment is made or removed only at a node inside the caller's user.update; a role without
    permissions is no free pass (an unknown, a foreign and the root node answer the same 403, nothing written)"""
    group_admin = await group_admin_of(db, tree.east)
    target = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    empty = await make_role(db, [])
    before = await assignment_ids(db, target)

    answers = []
    for kind, node_id in (("group", 999999), ("post", 999999), ("group", tree.west.id), ("post", tree.b.id), ("root", None)):
        refused = await client.post(assignments_url(target.id), json=body_of(empty, kind, node_id),
                                    headers=headers(group_admin))
        answers.append(problem(refused, 403, "grant_exceeds_own_permissions"))
    assert all(answer == answers[0] for answer in answers), answers
    assert answers[0]["missing"] == ["user.update"]
    assert await assignment_ids(db, target) == before
    assert not (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role_assignment"))).all()

    created = await client.post(assignments_url(target.id), json=body_of(empty, "group", tree.kharkiv.id),
                                headers=headers(group_admin))
    assert created.status_code == 201, created.text

    # One placed at West by someone else: the group admin cannot remove it either
    planted = await assign(db, target, empty, Node.group(tree.west.id))
    refused = await client.delete(assignment_url(target.id, planted.id), headers=headers(group_admin))
    assert problem(refused, 403, "grant_exceeds_own_permissions")["missing"] == ["user.update"]
    assert planted.id in await assignment_ids(db, target)


# ----------------------------------------------------------------- level, ceiling, nodes
async def test_system_role_never_a_tree_assignment(client, db, headers, tree):
    """A system role has no node: the tree assignment API refuses it at any node, the root included"""
    admin = await make_admin(db)
    target = await typed_user(db, UserType.ADMINISTRATOR, post_id=tree.a.id)
    taggers = await make_role(db, [Permissions.TAG_CREATE])
    for scope in (("post", tree.a.id), ("group", tree.east.id), ("root",)):
        refused = await client.post(assignments_url(target.id), json=body_of(taggers, *scope), headers=headers(admin))
        assert problem(refused, 409, "role_is_system")["role_id"] == taggers.id

    tagger = await make_role(db, [Permissions.TAG_VIEW])
    created = await client.post(assignments_url(target.id), json=body_of(tagger, "root"), headers=headers(admin))
    assert created.status_code == 201, created.text
    assert created.json()["scope"] == {"kind": "root", "id": None, "name": settings.app.name}
    assert created.json()["role"]["is_builtin"] is False


async def test_role_above_type_ceiling(client, db, headers, tree):
    admin = await make_admin(db)
    target = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    before = await assignment_ids(db, target)
    editor = await make_role(db, [Permissions.USER_UPDATE])

    refused = await client.post(assignments_url(target.id), json=body_of(editor, "post", tree.a.id), headers=headers(admin))
    body = problem(refused, 409, "role_exceeds_user_type")
    assert body["user_type"] == "operator" and body["excess_permissions"] == ["user.update"]
    assert await assignment_ids(db, target) == before


async def test_unknown_or_archived_role_is_not_found(client, db, headers, tree):
    admin = await make_admin(db)
    target = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    old = await make_role(db, [Permissions.POST_VIEW])
    await _archive(db, old)
    for role_id in (old.id, 999999):
        refused = await client.post(assignments_url(target.id), json={"role_id": role_id, "scope": scope("root")},
                                    headers=headers(admin))
        assert problem(refused, 404, "role_not_found")["role_id"] == role_id


async def test_assignment_to_archived_group(client, db, headers, tree):
    """Spec section 6 rule 7: an archived group or post takes no assignment"""
    admin = await make_admin(db)
    target = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    viewer = await builtin(db, UserType.VIEWER)
    old = await make_group(db, "Old", parent_id=tree.east.id)
    await _archive(db, old)
    gone = await make_post(db, "Gone", group_id=tree.kharkiv.id)
    await _archive(db, gone)

    for node_id in (old.id, 999999):
        problem(await client.post(assignments_url(target.id), json=body_of(viewer, "group", node_id), headers=headers(admin)),
                404, "scope_group_not_found")
    for node_id in (gone.id, 999999):
        body = problem(await client.post(assignments_url(target.id), json=body_of(viewer, "post", node_id),
                                         headers=headers(admin)), 404, "post_not_found")
        assert body["post_id"] == node_id
    assert len(await assignment_ids(db, target)) == 1


async def test_scope_kind_and_id_are_validated(client, db, headers, tree):
    admin = await make_admin(db)
    target = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    viewer = await builtin(db, UserType.VIEWER)
    cases = (
        ({"kind": "root", "id": tree.east.id}, "scope_id_not_allowed"),
        ({"kind": "group"}, "scope_id_required"),
        ({"kind": "post", "id": None}, "scope_id_required"),
        ({"kind": "planet", "id": 1}, "literal_error"),
        ({"kind": "group", "id": 0}, "greater_than"),
    )
    for sent, code in cases:
        refused = await client.post(assignments_url(target.id), json={"role_id": viewer.id, "scope": sent},
                                    headers=headers(admin))
        body = problem(refused, 422, "validation_error")
        assert body["errors"][0]["code"] == code, (sent, body)
        assert body["errors"][0]["pointer"].startswith("#/scope"), body


# ----------------------------------------------------------------- duplicates, the last assignment, the last admin
async def test_duplicate_and_last_assignment(client, db, headers, tree):
    admin = await make_admin(db)
    target = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    (only,) = await assignment_ids(db, target)
    viewer = await builtin(db, UserType.VIEWER)

    problem(await client.post(assignments_url(target.id), json=body_of(viewer, "post", tree.a.id), headers=headers(admin)),
            409, "assignment_exists")
    problem(await client.delete(assignment_url(target.id, only), headers=headers(admin)), 409, "last_assignment_required")
    assert await assignment_ids(db, target) == [only]

    second = await client.post(assignments_url(target.id), json=body_of(viewer, "group", tree.kharkiv.id),
                               headers=headers(admin))
    assert second.status_code == 201, second.text
    removed = await client.delete(assignment_url(target.id, only), headers=headers(admin))
    assert removed.status_code == 204, removed.text
    assert await assignment_ids(db, target) == [second.json()["id"]]

    problem(await client.delete(assignment_url(target.id, only), headers=headers(admin)), 404, "assignment_not_found")
    # An assignment of another user is not found through this user
    other = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    (foreign,) = await assignment_ids(db, other)
    problem(await client.delete(assignment_url(target.id, foreign), headers=headers(admin)), 404, "assignment_not_found")
    assert await assignment_ids(db, other) == [foreign]


async def test_the_last_assignment_counts_live_roles(client, db, headers, tree):
    """An assignment of an archived role grants nothing: the last assignment of a live role stays while only such
    assignments would remain; an assignment of an archived role goes while any other assignment remains"""
    admin = await make_admin(db)
    target = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    (live,) = await assignment_ids(db, target)
    old_role = await make_role(db, [Permissions.POST_VIEW])
    old_at_a = await assign(db, target, old_role, Node.post(tree.a.id))
    old_at_kharkiv = await assign(db, target, old_role, Node.group(tree.kharkiv.id))
    await _archive(db, old_role)

    listed = await client.get(assignments_url(target.id), headers=headers(admin))
    assert {item["id"]: item["role"]["is_deleted"] for item in listed.json()} == {
        live: False, old_at_a.id: True, old_at_kharkiv.id: True}

    problem(await client.delete(assignment_url(target.id, live), headers=headers(admin)), 409, "last_assignment_required")
    for removed in (old_at_a.id, old_at_kharkiv.id):
        response = await client.delete(assignment_url(target.id, removed), headers=headers(admin))
        assert response.status_code == 204, response.text
    assert await assignment_ids(db, target) == [live]

    # Left with assignments of archived roles only: any other one lets one go, the last one stays
    other = await make_user(db, post_id=tree.a.id, user_type=UserType.VIEWER)
    first = await assign(db, other, old_role, Node.post(tree.a.id))
    second = await assign(db, other, old_role, Node.group(tree.kharkiv.id))
    response = await client.delete(assignment_url(other.id, first.id), headers=headers(admin))
    assert response.status_code == 204, response.text
    problem(await client.delete(assignment_url(other.id, second.id), headers=headers(admin)), 409, "last_assignment_required")


async def test_a_concurrent_duplicate_is_refused_by_the_unique_index(client, db, headers, tree, monkeypatch):
    """Two requests pass the duplicate check together: the unique index answers assignment_exists, nothing written"""
    admin = await make_admin(db)
    target = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    before = await assignment_ids(db, target)

    async def missed(*args, **kwargs) -> bool:
        return False
    monkeypatch.setattr(assignment_rules, "_assignment_exists", missed)
    refused = await client.post(assignments_url(target.id), json=body_of(await builtin(db, UserType.VIEWER), "post", tree.a.id),
                                headers=headers(admin))
    problem(refused, 409, "assignment_exists")
    assert await assignment_ids(db, target) == before
    assert not (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role_assignment"))).all()


# ----------------------------------------------------------------- reads and archived users
async def test_assignments_of_an_archived_user(client, db, headers, tree):
    """GET like GET /users/{user_id}: an archived user only with user.archive covering them; changes: 404"""
    gone = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    (gone_assignment,) = await assignment_ids(db, gone)
    await _archive(db, gone)
    reader = await make_user(db, user_type=UserType.ANALYST)
    await assign(db, reader, await make_role(db, [Permissions.USER_VIEW, Permissions.USER_UPDATE]), Node.group(tree.east.id))
    problem(await client.get(assignments_url(gone.id), headers=headers(reader)), 404, "user_not_found")

    group_admin = await group_admin_of(db, tree.east)
    shown = await client.get(assignments_url(gone.id), headers=headers(group_admin))
    assert shown.status_code == 200 and [item["id"] for item in shown.json()] == [gone_assignment], shown.text

    viewer = await builtin(db, UserType.VIEWER)
    admin = await make_admin(db)
    problem(await client.post(assignments_url(gone.id), json=body_of(viewer, "group", tree.kharkiv.id),
                              headers=headers(admin)), 404, "user_not_found")
    problem(await client.delete(assignment_url(gone.id, gone_assignment), headers=headers(admin)), 404, "user_not_found")
    problem(await client.get(assignments_url(999999), headers=headers(admin)), 404, "user_not_found")
    problem(await client.delete(assignment_url(999999, gone_assignment), headers=headers(admin)), 404, "user_not_found")


async def test_own_assignments_are_visible_without_user_view_elsewhere(client, db, headers, tree):
    operator = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    other = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    reader = await make_user(db, user_type=UserType.ANALYST)
    await assign(db, reader, await make_role(db, [Permissions.USER_VIEW]), Node.post(tree.a.id))

    problem(await client.get(assignments_url(other.id), headers=headers(operator)), 403, "permission_denied")
    shown = await client.get(assignments_url(other.id), headers=headers(reader))
    assert shown.status_code == 200 and len(shown.json()) == 1, shown.text
    own = await client.get(assignments_url(reader.id), headers=headers(reader))
    assert own.status_code == 200 and own.json()[0]["scope"] == {"kind": "post", "id": tree.a.id, "name": "A"}


async def test_an_outsider_gets_the_same_403_for_an_archived_and_a_live_user(client, db, headers, tree):
    """Visibility before the state: the archive flag of a user outside the caller's user.view is not told"""
    live = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    gone = await typed_user(db, UserType.VIEWER, post_id=tree.a.id)
    await _archive(db, gone)
    outsider = await group_admin_of(db, tree.west)

    answers = []
    for user in (live, gone):
        body = problem(await client.get(assignments_url(user.id), headers=headers(outsider)), 403, "user_self_only")
        answers.append({key: value for key, value in body.items() if key != "instance"})
    assert answers[0] == answers[1]


# ----------------------------------------------------------------- effect of a group assignment
async def test_group_grant_reaches_posts_added_later(client, db, headers, tree):
    admin = await make_admin(db)
    operator_user = await typed_user(db, UserType.OPERATOR, post_id=tree.a.id)
    operator = await builtin(db, UserType.OPERATOR)
    created = await client.post(assignments_url(operator_user.id), json=body_of(operator, "group", tree.kharkiv.id),
                                headers=headers(admin))
    assert created.status_code == 201, created.text

    post = await client.post(V1.path(PostRoutes.ROOT), json={"name": "A2", "code": "A2CODE", "group_id": tree.kharkiv.id},
                             headers=headers(admin))
    assert post.status_code == 200, post.text
    ids = await make_dictionary(db)
    observed = await client.post(V1.path(ObservationRoutes.CREATE),
                                 json=observation_payload(post.json()["id"], ids["signal_type_id"]),
                                 headers=headers(operator_user))
    assert observed.status_code == 200, observed.text
    refused = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(tree.b.id, ids["signal_type_id"]),
                                headers=headers(operator_user))
    problem(refused, 403, "post_access_denied")
