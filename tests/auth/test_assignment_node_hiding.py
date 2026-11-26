"""Node names of other units never show in the answers about a user's assignments (owner decision L10, 2026-10-30).

An assignment at a node the caller cannot see stays listed with its role, the node hidden: scope null, node_visible
false. Can see (AccessControlService.node_named): a group inside a subtree where scope_group.view is held or on one
path with a node where user.view is held, a post where post.view is in effect, the root always. The caller's own assignments are always in full. A user list filtered
by a corps or a brigade the caller cannot see answers 403 permission_required_for_action, the same for an unknown id.

Tree of every test: East > Kharkiv > post A, East > brigade Bravo > post C; corps West > post B.
"""
from dataclasses import dataclass

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.builtin_roles import SENIOR_OPERATOR
from app.backend.auth.scope import Node, ROOT
from app.backend.config import settings
from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import AuthRoutes, ScopeGroupRoutes, SearchRoutes, UserRoutes
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user

P = Permissions
USERS = V1.path(UserRoutes.ROOT)
SEARCH_USERS = V1.path(SearchRoutes.USERS)
HIDDEN_NAMES = ("East", "Kharkiv", "Bravo")


@dataclass
class Tree:
    east: ScopeGroup
    kharkiv: ScopeGroup
    bravo: ScopeGroup
    west: ScopeGroup
    a: Post
    b: Post
    c: Post


@pytest.fixture
async def tree(db) -> Tree:
    east = await make_group(db, "East", kind="corps")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    bravo = await make_group(db, "Bravo", parent_id=east.id, kind="brigade")
    west = await make_group(db, "West", kind="corps")
    return Tree(east, kharkiv, bravo, west, await make_post(db, "A", group_id=kharkiv.id),
                await make_post(db, "B", group_id=west.id), await make_post(db, "C", group_id=bravo.id))


def assignments_url(user_id: int) -> str:
    return V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=user_id)


async def builtin(db, user_type: UserType) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == user_type.value))).one()


async def caller_with(db, perms, node: Node = ROOT) -> User:
    """A caller of type administrator holding exactly these permissions at the node"""
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, list(perms)), node)
    return user


async def home_user(db, post: Post) -> User:
    """A user of type viewer with the viewer base role at the home post"""
    user = await make_user(db, post_id=post.id, user_type=UserType.VIEWER)
    await assign(db, user, await builtin(db, UserType.VIEWER), Node.post(post.id))
    return user


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


def assert_no_hidden_name(response) -> None:
    for name in HIDDEN_NAMES:
        assert name not in response.text, response.text


# ----------------------------------------------------------------- the list
async def test_an_assignment_at_a_node_the_caller_cannot_see_stays_with_the_node_hidden(client, db, headers, tree):
    reader = await caller_with(db, [P.USER_VIEW, P.POST_VIEW, P.SCOPE_GROUP_VIEW], Node.group(tree.west.id))
    target = await home_user(db, tree.b)
    role = await make_role(db, [P.OBSERVATION_VIEW])
    at_group = await assign(db, target, role, Node.group(tree.kharkiv.id))
    at_corps = await assign(db, target, role, Node.group(tree.east.id))
    at_post = await assign(db, target, role, Node.post(tree.a.id))
    in_scope = await assign(db, target, role, Node.group(tree.west.id))

    response = await client.get(assignments_url(target.id), headers=headers(reader))
    assert response.status_code == 200, response.text
    assert_no_hidden_name(response)
    by_id = {item["id"]: item for item in response.json()}
    assert len(by_id) == 5
    for hidden in (at_group, at_corps, at_post):
        item = by_id[hidden.id]
        assert item["scope"] is None and item["node_visible"] is False, item
        assert item["role"]["id"] == role.id and item["created_at"], item
    assert by_id[in_scope.id]["scope"] == {"kind": "group", "id": tree.west.id, "name": "West"}
    assert by_id[in_scope.id]["node_visible"] is True
    home = next(item for item in response.json() if item["role"]["name"] == "viewer")
    assert home["scope"] == {"kind": "post", "id": tree.b.id, "name": "B"} and home["node_visible"] is True


async def test_the_root_is_visible_to_everyone_who_may_read_the_record(client, db, headers, tree):
    reader = await caller_with(db, [P.USER_VIEW], Node.post(tree.b.id))
    target = await home_user(db, tree.b)
    at_root = await assign(db, target, await make_role(db, [P.OBSERVATION_VIEW]), ROOT)

    listed = (await client.get(assignments_url(target.id), headers=headers(reader))).json()
    item = next(item for item in listed if item["id"] == at_root.id)
    assert item["scope"] == {"kind": "root", "id": None, "name": settings.app.name} and item["node_visible"] is True


async def test_a_group_follows_scope_group_view_and_a_post_follows_post_view(client, db, headers, tree):
    target = await home_user(db, tree.b)
    role = await make_role(db, [P.OBSERVATION_VIEW])
    at_group = await assign(db, target, role, Node.group(tree.kharkiv.id))
    at_post = await assign(db, target, role, Node.post(tree.b.id))

    async def shown(reader):
        listed = (await client.get(assignments_url(target.id), headers=headers(reader))).json()
        return {item["id"]: item["node_visible"] for item in listed}

    groups_only = await shown(await caller_with(db, [P.USER_VIEW, P.SCOPE_GROUP_VIEW]))
    assert (groups_only[at_group.id], groups_only[at_post.id]) == (True, False)
    posts_only = await shown(await caller_with(db, [P.USER_VIEW, P.POST_VIEW], Node.group(tree.west.id)))
    assert (posts_only[at_group.id], posts_only[at_post.id]) == (False, True)


async def test_user_view_names_the_units_on_its_path_never_a_sibling(client, db, headers, tree):
    """user.view at a brigade names the corps above it (the records already do), not a group beside it"""
    target = await home_user(db, tree.c)
    role = await make_role(db, [P.OBSERVATION_VIEW])
    at_corps = await assign(db, target, role, Node.group(tree.east.id))
    at_brigade = await assign(db, target, role, Node.group(tree.bravo.id))
    at_sibling = await assign(db, target, role, Node.group(tree.kharkiv.id))
    reader = await caller_with(db, [P.USER_VIEW], Node.group(tree.bravo.id))

    listed = {item["id"]: item for item in
              (await client.get(assignments_url(target.id), headers=headers(reader))).json()}
    assert listed[at_corps.id]["node_visible"] is True and listed[at_brigade.id]["node_visible"] is True
    assert listed[at_sibling.id]["scope"] is None and listed[at_sibling.id]["node_visible"] is False
    outsider = await client.get(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=tree.bravo.id), headers=headers(reader))
    assert outsider.status_code in (403, 404), "reading the group itself stays with scope_group.view"


async def test_an_archived_node_inside_the_scope_stays_named(client, db, headers, tree):
    """Archived or not is a state of the node; visibility is the caller's scope (a group grant keeps covering it)"""
    target = await home_user(db, tree.b)
    at_group = await assign(db, target, await make_role(db, [P.OBSERVATION_VIEW]), Node.group(tree.kharkiv.id))
    tree.kharkiv.is_deleted = True
    db.add(tree.kharkiv)
    await db.commit()
    reader = await caller_with(db, [P.USER_VIEW, P.SCOPE_GROUP_VIEW])

    listed = {item["id"]: item for item in
              (await client.get(assignments_url(target.id), headers=headers(reader))).json()}
    assert listed[at_group.id]["scope"]["name"] == "Kharkiv" and listed[at_group.id]["node_visible"] is True


async def test_own_assignments_are_always_in_full(client, db, headers, tree):
    user = await make_user(db, post_id=tree.b.id, user_type=UserType.VIEWER)
    await assign(db, user, await make_role(db, [P.OBSERVATION_VIEW]), Node.group(tree.kharkiv.id))

    expected = {"kind": "group", "id": tree.kharkiv.id, "name": "Kharkiv"}
    me = await client.get(V1.path(AuthRoutes.ME), headers=headers(user))
    assert me.status_code == 200, me.text
    assert [(item["scope"], item["node_visible"]) for item in me.json()["assignments"]] == [(expected, True)]

    reader = await make_user(db, post_id=tree.b.id, user_type=UserType.ANALYST)
    await assign(db, reader, await make_role(db, [P.USER_VIEW]), Node.group(tree.kharkiv.id))
    own = await client.get(assignments_url(reader.id), headers=headers(reader))
    assert own.status_code == 200, own.text
    assert [(item["scope"], item["node_visible"]) for item in own.json()] == [(expected, True)]


# ----------------------------------------------------------------- the writes
async def test_a_new_assignment_at_a_node_the_caller_cannot_see_answers_hidden(client, db, headers, tree):
    """The caller may place the role there (user.update and its permissions cover the node) and named only its id:
    the answer tells no more than the list"""
    caller = await caller_with(db, [P.USER_VIEW], Node.group(tree.west.id))
    await assign(db, caller, await make_role(db, [P.USER_UPDATE, P.OBSERVATION_VIEW, P.MEDIA_VIEW]), ROOT)
    target = await make_user(db, post_id=tree.b.id, user_type=UserType.VIEWER)
    await assign(db, target, await make_role(db, [P.MEDIA_VIEW]), ROOT)
    role = await make_role(db, [P.OBSERVATION_VIEW])

    created = await client.post(assignments_url(target.id), headers=headers(caller), json={
        "role_id": role.id, "scope": {"kind": "group", "id": tree.kharkiv.id}})
    assert created.status_code == 201, created.text
    assert_no_hidden_name(created)
    body = created.json()
    assert body["scope"] is None and body["node_visible"] is False and body["role"]["id"] == role.id

    listed = await client.get(assignments_url(target.id), headers=headers(caller))
    assert next(item for item in listed.json() if item["id"] == body["id"]) == body


async def test_a_new_assignment_at_a_visible_node_answers_in_full(client, db, headers, tree):
    caller = await caller_with(db, [P.USER_VIEW, P.USER_UPDATE, P.OBSERVATION_VIEW, P.SCOPE_GROUP_VIEW])
    target = await make_user(db, post_id=tree.b.id, user_type=UserType.VIEWER)
    await assign(db, target, await make_role(db, [P.OBSERVATION_VIEW]), ROOT)
    role = await make_role(db, [P.OBSERVATION_VIEW])

    created = await client.post(assignments_url(target.id), headers=headers(caller), json={
        "role_id": role.id, "scope": {"kind": "group", "id": tree.kharkiv.id}})
    assert created.status_code == 201, created.text
    assert created.json()["scope"]["name"] == "Kharkiv" and created.json()["node_visible"] is True


async def test_removing_an_assignment_tells_nothing_about_its_node(client, db, headers, tree):
    target = await make_user(db, post_id=tree.b.id, user_type=UserType.VIEWER)
    await assign(db, target, await make_role(db, [P.MEDIA_VIEW]), ROOT)
    hidden = await assign(db, target, await make_role(db, [P.OBSERVATION_VIEW]), Node.group(tree.kharkiv.id))
    url = V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=target.id, assignment_id=hidden.id)

    too_little = await caller_with(db, [P.USER_VIEW, P.USER_UPDATE, P.MEDIA_VIEW], Node.post(tree.b.id))
    refused = await client.delete(url, headers=headers(too_little))
    assert refused.status_code == 403, refused.text
    assert_no_hidden_name(refused)

    caller = await caller_with(db, [P.USER_VIEW, P.USER_UPDATE, P.OBSERVATION_VIEW, P.MEDIA_VIEW])
    removed = await client.delete(url, headers=headers(caller))
    assert removed.status_code == 204 and removed.content == b"", removed.text


async def test_the_ceiling_refusal_hides_a_node_the_caller_cannot_see(client, db, headers, tree):
    """PATCH /users/{id} with user_type lists the assignments that exceed the new type: the same rule"""
    target = await make_user(db, post_id=tree.b.id, user_type=UserType.SENIOR_OPERATOR)
    await assign(db, target, await builtin(db, UserType.SENIOR_OPERATOR), Node.post(tree.b.id))
    media_editor = await make_role(db, [P.MEDIA_UPDATE])
    at_group = await assign(db, target, media_editor, Node.group(tree.kharkiv.id))
    at_post = await assign(db, target, media_editor, Node.post(tree.b.id))
    caller = await caller_with(db, [P.USER_VIEW], Node.group(tree.west.id))
    await assign(db, caller, await make_role(db, {*SENIOR_OPERATOR, P.USER_UPDATE.value, P.MEDIA_UPDATE.value} - {
        P.USER_VIEW.value}), ROOT)

    refused = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(caller),
                                 data={"user_type": "operator"})
    body = problem(refused, 409, "user_type_ceiling_exceeded")
    assert_no_hidden_name(refused)
    items = {item["id"]: item for item in body["assignments"]}
    assert items[at_group.id]["scope"] is None and items[at_group.id]["node_visible"] is False
    assert items[at_group.id]["role"]["id"] == media_editor.id
    assert items[at_group.id]["excess_permissions"] == ["media.update"]
    assert items[at_post.id]["scope"] == {"kind": "post", "id": tree.b.id, "name": "B"}
    assert items[at_post.id]["node_visible"] is True


# ----------------------------------------------------------------- the foreign filter
@pytest.mark.parametrize("path", [USERS, SEARCH_USERS])
@pytest.mark.parametrize("membership", ["any", "home", "assigned"])
@pytest.mark.parametrize("field,unit", [("brigade_id", "bravo"), ("corps_id", "east")])
async def test_a_filter_naming_a_unit_the_caller_cannot_see_is_refused(client, db, headers, tree, path, membership,
                                                                       field, unit):
    caller = await caller_with(db, [P.USER_VIEW, P.SCOPE_GROUP_VIEW], Node.group(tree.west.id))
    refused = await client.get(path, headers=headers(caller),
                               params={field: getattr(tree, unit).id, "membership": membership})
    body = problem(refused, 403, "permission_required_for_action")
    assert body["permission"] == "scope_group.view" and body["missing"] == ["scope_group.view"]
    assert_no_hidden_name(refused)


@pytest.mark.parametrize("path", [USERS, SEARCH_USERS])
async def test_a_refused_filter_answers_alike_for_an_unknown_id(client, db, headers, tree, path):
    """Scope before existence: an outsider learns nothing about ids (a live unit, an unknown id, an impossible id)"""
    caller = await caller_with(db, [P.USER_VIEW, P.SCOPE_GROUP_VIEW], Node.group(tree.west.id))
    answers = []
    for unit_id in (tree.bravo.id, 987654, 0, -1):
        body = problem(await client.get(path, headers=headers(caller), params={"brigade_id": unit_id}), 403,
                       "permission_required_for_action")
        answers.append({key: value for key, value in body.items() if key != "instance"})
    assert all(answer == answers[0] for answer in answers)


@pytest.mark.parametrize("path", [USERS, SEARCH_USERS])
async def test_a_user_view_holder_filters_by_the_units_above_its_post(client, db, headers, tree, path):
    """The user records already name them; the rule of GET /brigades/{id} itself does not change"""
    caller = await caller_with(db, [P.USER_VIEW], Node.post(tree.c.id))
    for field, unit in (("brigade_id", tree.bravo), ("corps_id", tree.east)):
        response = await client.get(path, headers=headers(caller), params={field: unit.id})
        assert response.status_code == 200, response.text
    problem(await client.get(path, headers=headers(caller), params={"brigade_id": tree.kharkiv.id}), 403,
            "permission_required_for_action")


@pytest.mark.parametrize("path", [USERS, SEARCH_USERS])
async def test_a_filter_on_a_unit_the_caller_sees_works(client, db, headers, tree, path):
    caller = await caller_with(db, [P.USER_VIEW, P.SCOPE_GROUP_VIEW], Node.group(tree.bravo.id))
    target = await home_user(db, tree.c)
    response = await client.get(path, headers=headers(caller), params={"brigade_id": tree.bravo.id})
    assert response.status_code == 200, response.text
    found = response.json()["items"] if path == SEARCH_USERS else response.json()
    assert sorted(user["id"] for user in found) == sorted([caller.id, target.id])     # the caller's role sits there


@pytest.mark.parametrize("path", [USERS, SEARCH_USERS])
async def test_at_the_root_every_id_stays_a_filter_that_finds_nothing(client, db, headers, tree, path):
    """scope_group.view at the root sees every node: an unknown id, an impossible one or an id of another kind is a
    list without rows, as before"""
    h = headers(await make_admin(db))
    for params in ({"brigade_id": 987654}, {"brigade_id": 0}, {"corps_id": -1}, {"brigade_id": tree.east.id}):
        response = await client.get(path, headers=h, params=params)
        assert response.status_code == 200, response.text
        assert (response.json()["items"] if path == SEARCH_USERS else response.json()) == []


async def test_the_post_filter_keeps_its_answer(client, db, headers, tree):
    caller = await caller_with(db, [P.USER_VIEW, P.SCOPE_GROUP_VIEW], Node.group(tree.west.id))
    problem(await client.get(USERS, headers=headers(caller), params={"post_id": tree.a.id}), 403, "post_access_denied")
