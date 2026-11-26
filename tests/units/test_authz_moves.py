"""Authorization fixes of 2026-10-29 (audit findings H1, M1, L1).

H1: a move of a post, a group or a brigade puts every grant of the destination's ancestors in effect at the moved
subtree, and the restore of an archived post or group revives the grants at that node. Both are refused unless the
caller could grant those assignments at the node (auth/privileges.py check_move, check_node_restore; 403
grant_exceeds_own_permissions), like the assignment API refuses the same grant.

M1: delegation judges a target user's assignments by their role permissions, not through the target's current
boundary, and a type change needs the caller to cover what the new boundary puts in effect.

L1: every user write route needs user.view in effect at the target first: the same 403 user_self_only as
GET /users/{id}.
"""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.context import AuthContext, load_scope_tree
from app.backend.auth.privileges import role_permission_names
from app.backend.auth.scope import Grant, Node, ROOT
from app.backend.enums.general import BuiltinRole, Permissions as P, RoleKind, UserType
from app.backend.enums.routes import (
    AuthRoutes, BrigadeRoutes, CorpsRoutes, PostRoutes, ScopeGroupRoutes, UserRoutes,
)
from app.backend.errors.problems import ApiError
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User
from tests import helpers as factories
from tests.helpers import make_admin, make_group, make_post, make_role

STRUCTURE = [P.POST_VIEW, P.POST_UPDATE, P.SCOPE_GROUP_VIEW, P.SCOPE_GROUP_UPDATE]


async def _builtin(db, key: BuiltinRole) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == key.value))).one()


async def _user(db, user_type: UserType, post_id=None) -> User:
    user = User(username=f"u{factories._next()}", username_alias="x", post_id=post_id, user_type=user_type.value)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    factories._EXPLICIT_TYPES.add(user.id)
    return user


async def _raw_assign(db, user: User, role: Role, node: Node) -> RoleAssignment:
    """A role_assignments row written without the API (and without the factories' boundary guard)"""
    row = RoleAssignment(user_id=user.id, role_id=role.id, **scope_columns(node))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _admin_at(db, node: Node, post_id=None) -> User:
    """An administrator (type) holding the built-in administrator role at node"""
    user = await _user(db, UserType.ADMINISTRATOR, post_id=post_id)
    await _raw_assign(db, user, await _builtin(db, BuiltinRole.ADMINISTRATOR), node)
    return user


async def _can(client, headers, user, perms, post_id=None) -> bool:
    body = {"all": [p.value for p in perms]}
    if post_id is not None:
        body["post_id"] = post_id
    response = await client.post(V1.path(AuthRoutes.CHECK_PERMISSION), json=body, headers=headers(user))
    assert response.status_code == 200, response.text
    return response.json()["allowed"]


def _grant_refused(response, *missing: P) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "grant_exceeds_own_permissions", response.text
    for permission in missing:
        assert permission.value in response.json()["missing"], response.text


async def _post_group(db, post_id: int):
    await db.commit()   # a fresh read of what the request committed
    return (await db.exec(select(Post.group_id).where(Post.id == post_id))).one()


async def _group_parent(db, group_id: int):
    await db.commit()   # a fresh read of what the request committed
    return (await db.exec(select(ScopeGroup.parent_id).where(ScopeGroup.id == group_id))).one()


# ---------------------------------------------------------------- H1: moves
async def test_post_move_into_own_subtree_is_refused(client, db, headers):
    """Audit H1: an administrator of one group with a post editor role at the root moves a foreign post into the
    group. Before the fix this answered 200 and gave the mover every tree permission at the post."""
    mine = await make_group(db, "Mine")
    theirs = await make_group(db, "Theirs")
    post = await make_post(db, "Foreign", group_id=theirs.id)
    operator = await _user(db, UserType.OPERATOR, post_id=post.id)
    await _raw_assign(db, operator, await _builtin(db, BuiltinRole.OPERATOR), Node.post(post.id))
    await make_admin(db)

    mover = await _admin_at(db, Node.group(mine.id))
    await _raw_assign(db, mover, await make_role(db, [P.POST_VIEW, P.POST_UPDATE]), ROOT)
    assert not await _can(client, headers, mover, [P.USER_UPDATE], post.id)

    moved = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": mine.id},
                             headers=headers(mover))
    _grant_refused(moved, P.USER_UPDATE, P.CERTIFICATE_CREATE, P.OBSERVATION_VIEW)
    assert await _post_group(db, post.id) == theirs.id
    assert not await _can(client, headers, mover, [P.USER_UPDATE], post.id)
    user_url = V1.path(UserRoutes.USER_BY_ID, user_id=operator.id)
    assert (await client.get(user_url, headers=headers(mover))).status_code == 403


async def test_assignment_api_refuses_the_same_grant(client, db, headers):
    """Control: the assignment API refuses the same mover the administrator role at that post, for a user the mover
    manages (a user of the mover's group) as for the mover itself"""
    mine = await make_group(db, "Mine2")
    theirs = await make_group(db, "Theirs2")
    post = await make_post(db, "Foreign2", group_id=theirs.id)
    own_post = await make_post(db, "Own2", group_id=mine.id)
    await make_admin(db)
    mover = await _admin_at(db, Node.group(mine.id))
    await _raw_assign(db, mover, await make_role(db, [P.POST_VIEW, P.POST_UPDATE]), ROOT)
    managed = await _admin_at(db, Node.post(own_post.id), post_id=own_post.id)
    body = {"role_id": (await _builtin(db, BuiltinRole.ADMINISTRATOR)).id, "scope": {"kind": "post", "id": post.id}}
    _grant_refused(await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=managed.id),
                                     headers=headers(mover), json=body), P.USER_UPDATE)
    response = await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=mover.id), headers=headers(mover),
                                 json=body)
    assert response.status_code == 403, response.text


async def test_group_move_into_own_subtree_is_refused(client, db, headers):
    """Audit H1 with a plain group moved under the mover's group"""
    mine = await make_group(db, "Mine3")
    theirs = await make_group(db, "Theirs3")
    post = await make_post(db, "Foreign3", group_id=theirs.id)
    await make_admin(db)
    mover = await _admin_at(db, Node.group(mine.id))
    await _raw_assign(db, mover, await make_role(db, STRUCTURE), ROOT)
    moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=theirs.id),
                               json={"parent_id": mine.id}, headers=headers(mover))
    _grant_refused(moved, P.USER_UPDATE, P.CERTIFICATE_CREATE)
    assert await _group_parent(db, theirs.id) is None
    assert not await _can(client, headers, mover, [P.USER_UPDATE, P.CERTIFICATE_CREATE], post.id)


async def test_brigade_move_into_own_corps_is_refused(client, db, headers):
    """Audit H1 with a brigade moved into the corps the mover administers"""
    corps_mine = await make_group(db, "CorpsMine", kind="corps")
    corps_theirs = await make_group(db, "CorpsTheirs", kind="corps")
    brigade = await make_group(db, "BrigadeTheirs", parent_id=corps_theirs.id, kind="brigade")
    post = await make_post(db, "InBrigade", group_id=brigade.id)
    await make_admin(db)
    mover = await _admin_at(db, Node.group(corps_mine.id))
    await _raw_assign(db, mover, await make_role(db, STRUCTURE), ROOT)
    moved = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                               json={"corps_id": corps_mine.id}, headers=headers(mover))
    _grant_refused(moved, P.USER_UPDATE)
    assert await _group_parent(db, brigade.id) == corps_theirs.id
    assert not await _can(client, headers, mover, [P.USER_UPDATE], post.id)


async def test_post_move_under_another_administrator_is_refused(client, db, headers):
    """Another user's grants count too: a structure maintainer (post.view and post.update at the root) cannot put a
    post under a group whose administrator would gain rights the maintainer could not grant there"""
    target_group = await make_group(db, "Commanded")
    post = await make_post(db, "Loose")
    await make_admin(db)
    commander = await _admin_at(db, Node.group(target_group.id))
    maintainer = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, maintainer, await make_role(db, [P.POST_VIEW, P.POST_UPDATE]), ROOT)

    moved = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": target_group.id},
                             headers=headers(maintainer))
    _grant_refused(moved, P.USER_UPDATE)
    assert await _post_group(db, post.id) is None
    assert not await _can(client, headers, commander, [P.USER_UPDATE], post.id)


async def test_moves_the_caller_could_grant_are_accepted(client, db, headers):
    """The administrator of a corps moves a post between two of its brigades, each with an administrator of its own:
    the corps administrator holds every tree permission there. A move up, to an ancestor, adds nothing."""
    corps = await make_group(db, "CorpsOwn", kind="corps")
    brigade_a = await make_group(db, "BrigadeA", parent_id=corps.id, kind="brigade")
    brigade_b = await make_group(db, "BrigadeB", parent_id=corps.id, kind="brigade")
    post = await make_post(db, "Moving", group_id=brigade_a.id)
    await make_admin(db)
    await _admin_at(db, Node.group(brigade_a.id))
    commander_b = await _admin_at(db, Node.group(brigade_b.id))
    corps_admin = await _admin_at(db, Node.group(corps.id))

    url = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)
    moved = await client.put(url, json={"group_id": brigade_b.id}, headers=headers(corps_admin))
    assert moved.status_code == 200, moved.text
    assert await _can(client, headers, commander_b, [P.USER_UPDATE], post.id)

    # Up into the corps: the corps and above already applied at the post
    up = await client.put(url, json={"group_id": corps.id}, headers=headers(corps_admin))
    assert up.status_code == 200, up.text

    # A plain post maintainer moves a post out of a commanded group to the root: nothing new applies
    group = await make_group(db, "Commanded2")
    other = await make_post(db, "Leaving", group_id=group.id)
    await _admin_at(db, Node.group(group.id))
    maintainer = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, maintainer, await make_role(db, [P.POST_VIEW, P.POST_UPDATE]), ROOT)
    out = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=other.id), json={"group_id": None},
                           headers=headers(maintainer))
    assert out.status_code == 200, out.text


# ---------------------------------------------------------------- H1: node restores
async def _archive(db, row) -> None:
    row.is_deleted = True
    db.add(row)
    await db.commit()


async def test_post_restore_reviving_grants_beyond_the_caller_is_refused(client, db, headers):
    """Audit H1: the restore of a post revives the grants made on it; the caller must be able to grant them there"""
    post = await make_post(db, "Archived")
    await make_admin(db)
    holder = await _user(db, UserType.ADMINISTRATOR, post_id=post.id)
    await _raw_assign(db, holder, await _builtin(db, BuiltinRole.ADMINISTRATOR), Node.post(post.id))
    await _archive(db, post)
    archivist = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, archivist, await make_role(db, [P.POST_VIEW, P.POST_ARCHIVE]), ROOT)

    url = V1.path(PostRoutes.POST_RESTORE, post_id=post.id)
    _grant_refused(await client.post(url, headers=headers(archivist)), P.USER_UPDATE)
    await db.commit()   # a fresh read of what the request committed
    assert (await db.exec(select(Post.is_deleted).where(Post.id == post.id))).one() is True

    restored = await client.post(url, headers=headers(await make_admin(db)))
    assert restored.status_code == 200, restored.text


async def test_group_restore_reviving_grants_beyond_the_caller_is_refused(client, db, headers):
    """The same for a plain group and a brigade"""
    group = await make_group(db, "ArchivedGroup")
    corps = await make_group(db, "CorpsR", kind="corps")
    brigade = await make_group(db, "BrigadeR", parent_id=corps.id, kind="brigade")
    await make_admin(db)
    await _admin_at(db, Node.group(group.id))
    await _admin_at(db, Node.group(brigade.id))
    await _archive(db, group)
    await _archive(db, brigade)
    archivist = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, archivist, await make_role(db, [P.SCOPE_GROUP_VIEW, P.SCOPE_GROUP_ARCHIVE]), ROOT)

    _grant_refused(await client.post(V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=group.id),
                                     headers=headers(archivist)), P.USER_UPDATE)
    _grant_refused(await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade.id),
                                     headers=headers(archivist)), P.USER_UPDATE)

    # The administrator of the corps could grant the brigade's administrator role: the restore goes through
    corps_admin = await _admin_at(db, Node.group(corps.id))
    await _raw_assign(db, corps_admin, await make_role(db, [P.SCOPE_GROUP_VIEW, P.SCOPE_GROUP_ARCHIVE]), ROOT)
    restored = await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade.id),
                                 headers=headers(corps_admin))
    assert restored.status_code == 200, restored.text


async def test_corps_restore_reviving_grants_beyond_the_caller_is_refused(client, db, headers):
    corps = await make_group(db, "CorpsArchived", kind="corps")
    await make_admin(db)
    await _admin_at(db, Node.group(corps.id))
    await _archive(db, corps)
    archivist = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, archivist, await make_role(db, [P.SCOPE_GROUP_VIEW, P.SCOPE_GROUP_ARCHIVE]), ROOT)
    _grant_refused(await client.post(V1.path(CorpsRoutes.CORPS_RESTORE, corps_id=corps.id),
                                     headers=headers(archivist)), P.USER_UPDATE)


# ---------------------------------------------------------------- M1: the target's grants, unfiltered
async def _operator_with_left_out_grant(db, post_id: int) -> User:
    """An operator with a grant outside the operator boundary at the root (an old backup, a migration, a narrowed
    boundary): left out at every request"""
    target = await _user(db, UserType.OPERATOR, post_id=post_id)
    await _raw_assign(db, target, await _builtin(db, BuiltinRole.OPERATOR), Node.post(post_id))
    await _raw_assign(db, target, await make_role(db, [P.USER_VIEW, P.USER_UPDATE, P.USER_CREATE]), ROOT)
    return target


async def test_type_change_cannot_revive_permissions_the_caller_does_not_hold(client, db, headers):
    """Audit M1: before the fix the brigade administrator's type change answered 200 and the target held user.* at
    the root"""
    brigade = await make_group(db, "Brigade")
    elsewhere = await make_post(db, "Elsewhere")
    post = await make_post(db, "Inside", group_id=brigade.id)
    await make_admin(db)
    target = await _operator_with_left_out_grant(db, post.id)
    assert not await _can(client, headers, target, [P.USER_UPDATE], elsewhere.id)
    caller = await _admin_at(db, Node.group(brigade.id))

    changed = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id),
                                 data={"user_type": "administrator"}, headers=headers(caller))
    assert changed.status_code == 403, changed.text
    assert changed.json()["code"] == "target_user_more_privileged", changed.text
    assert P.USER_UPDATE.value in changed.json()["missing"]
    await db.commit()   # a fresh read of what the request committed
    assert (await db.exec(select(User.user_type).where(User.id == target.id))).one() == UserType.OPERATOR.value
    assert not await _can(client, headers, target, [P.USER_UPDATE], elsewhere.id)

    # Any other change of the target is judged the same way
    renamed = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id),
                                 data={"username_alias": "y"}, headers=headers(caller))
    assert renamed.status_code == 403, renamed.text
    assert renamed.json()["code"] == "target_user_more_privileged", renamed.text


async def test_type_change_needs_what_the_new_boundary_puts_in_effect(db):
    """change_user_type itself: a grant left out today that the new type switches on needs the caller to hold its
    permissions at its node, also for a change of oneself (check_target_user does not look at one's own grants)"""
    from app.backend.auth.assignments import change_user_type
    post = await make_post(db, "Home")
    target = await _operator_with_left_out_grant(db, post.id)
    admin_role = await _builtin(db, BuiltinRole.ADMINISTRATOR)
    names = await role_permission_names(db, [admin_role.id])
    # The caller (the target itself) holds every tree permission at the home post, nothing at the root
    auth = AuthContext(user=target, grants=(Grant(admin_role.id, "administrator", "Administrator", True,
                                                   Node.post(post.id), names),),
                       tree=await load_scope_tree(db))
    try:
        await change_user_type(db, auth, target, UserType.ADMINISTRATOR)
    except ApiError as exc:
        assert exc.status_code == 403
        assert exc.code.value == "grant_exceeds_own_permissions"
        assert P.USER_CREATE.value in exc.params["missing"]
    else:
        raise AssertionError("the type change switched on user.* at the root")
    await db.rollback()


# ---------------------------------------------------------------- L1: user writes need user.view at the target
async def test_user_writes_without_user_view_at_the_target_answer_like_get(client, db, headers):
    """Audit L1: user.update (and user.archive) at the target's post, user.view only elsewhere. Before the fix PATCH
    answered 200 with the full record while GET answered 403."""
    hidden = await make_post(db, "Hidden")
    seen = await make_post(db, "Seen")
    await make_admin(db)
    target = await _user(db, UserType.VIEWER, post_id=hidden.id)
    target_assignment = await _raw_assign(db, target, await _builtin(db, BuiltinRole.VIEWER), Node.post(hidden.id))
    caller = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, caller, await make_role(db, [
        P.USER_UPDATE, P.USER_ARCHIVE, P.POST_VIEW, P.OBSERVATION_VIEW, P.MEDIA_VIEW, P.CATEGORY_VIEW,
        P.SIGNAL_TYPE_VIEW, P.TAG_VIEW]), Node.post(hidden.id))
    await _raw_assign(db, caller, await make_role(db, [P.USER_VIEW]), Node.post(seen.id))
    tag_editor = await _builtin(db, BuiltinRole.TAG_EDITOR)

    def self_only(response) -> None:
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "user_self_only", response.text

    user_url = V1.path(UserRoutes.USER_BY_ID, user_id=target.id)
    self_only(await client.get(user_url, headers=headers(caller)))
    self_only(await client.patch(user_url, data={"username_alias": "changed"}, headers=headers(caller)))
    self_only(await client.post(V1.path(UserRoutes.USER_ARCHIVE, user_id=target.id), headers=headers(caller)))
    self_only(await client.post(V1.path(UserRoutes.USER_RESTORE, user_id=target.id), headers=headers(caller)))
    self_only(await client.post(V1.path(UserRoutes.USER_SESSIONS_END, user_id=target.id), headers=headers(caller)))
    self_only(await client.post(V1.path(UserRoutes.USER_SESSION_END, user_id=target.id, session_id="none"),
                                headers=headers(caller)))
    self_only(await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id), headers=headers(caller),
                                json={"role_id": (await _builtin(db, BuiltinRole.VIEWER)).id,
                                      "scope": {"kind": "post", "id": seen.id}}))
    self_only(await client.delete(V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=target.id,
                                          assignment_id=target_assignment.id), headers=headers(caller)))
    # The system-assignment writes need user.update at the root (L8): below it the gate answers first, and with it
    # user.view at the target still decides
    system_writes = (
        lambda caller_headers: client.post(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENTS, user_id=target.id),
                                           headers=caller_headers, json={"role_id": tag_editor.id}),
        lambda caller_headers: client.delete(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENT_BY_ID, user_id=target.id,
                                                     assignment_id=1), headers=caller_headers),
    )
    for write in system_writes:
        denied = await write(headers(caller))
        assert denied.status_code == 403 and denied.json()["code"] == "permission_denied", denied.text
    at_root = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, at_root, await make_role(db, [P.USER_UPDATE]), ROOT)
    await _raw_assign(db, at_root, await make_role(db, [P.USER_VIEW]), Node.post(seen.id))
    for write in system_writes:
        self_only(await write(headers(at_root)))
    await db.commit()   # a fresh read of what the request committed
    assert (await db.exec(select(User.username_alias).where(User.id == target.id))).one() == "x"


# ---------------------------------------------------------------- review of 367bf02: what can never act, per user
async def _dead_role(db, dead: str) -> Role:
    """A role in the tree table that holds observation.view and something no type can put in effect: a name the
    catalog does not know, a system permission in a tree role, or the role itself of the system kind"""
    if dead == "unknown":
        role = await make_role(db, [P.OBSERVATION_VIEW])
        stale = Permission(name="legacy.removed", level="tree")
        db.add(stale)
        await db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=stale.id))
        await db.commit()
        return role
    if dead == "other_level":
        return await make_role(db, [P.OBSERVATION_VIEW, P.AUDIT_VIEW], kind=RoleKind.TREE, allow_mixed=True)
    return await make_role(db, [P.AUDIT_VIEW], kind=RoleKind.SYSTEM)    # a system role in role_assignments


@pytest.mark.parametrize("dead", ["unknown", "other_level", "other_kind"])
async def test_names_no_type_can_put_in_effect_block_nobody(client, db, headers, dead):
    """A dead assignment (what no type ever puts in effect) does not lock out the system administrator or the
    administrator above the node: they edit the user, end its sessions, move a post into the node and remove the
    dead assignment"""
    corps = await make_group(db, f"CorpsDead{dead}", kind="corps")
    b1 = await make_group(db, f"B1{dead}", parent_id=corps.id, kind="brigade")
    b2 = await make_group(db, f"B2{dead}", parent_id=corps.id, kind="brigade")
    home = await make_post(db, f"Home{dead}", group_id=b1.id)
    moving = await make_post(db, f"Q{dead}", group_id=b1.id)
    target = await _user(db, UserType.OPERATOR, post_id=home.id)
    await _raw_assign(db, target, await _builtin(db, BuiltinRole.OPERATOR), Node.post(home.id))
    dead_row = await _raw_assign(db, target, await _dead_role(db, dead), Node.group(b2.id))
    system_admin = await make_admin(db)
    corps_admin = await _admin_at(db, Node.group(corps.id))

    user_url = V1.path(UserRoutes.USER_BY_ID, user_id=target.id)
    post_url = V1.path(PostRoutes.POST_BY_ID, post_id=moving.id)
    for actor in (system_admin, corps_admin):
        edited = await client.patch(user_url, data={"username_alias": f"by {actor.id}"}, headers=headers(actor))
        assert edited.status_code == 200, edited.text
        ended = await client.post(V1.path(UserRoutes.USER_SESSIONS_END, user_id=target.id), headers=headers(actor))
        assert ended.status_code == 200, ended.text
        there = await client.put(post_url, json={"group_id": b2.id}, headers=headers(actor))
        assert there.status_code == 200, there.text
        back = await client.put(post_url, json={"group_id": b1.id}, headers=headers(actor))
        assert back.status_code == 200, back.text
    removed = await client.delete(V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=target.id,
                                          assignment_id=dead_row.id), headers=headers(corps_admin))
    assert removed.status_code == 204, removed.text


async def test_a_move_counts_what_each_user_newly_gets(client, db, headers):
    """Review probe: X holds administrator at both brigades; a maintainer (post.view and post.update at the corps)
    moves a post from one to the other: nobody gains anything at the post. Another user holding administrator at
    the destination alone still blocks."""
    corps = await make_group(db, "CorpsDup", kind="corps")
    b1 = await make_group(db, "Dup1", parent_id=corps.id, kind="brigade")
    b2 = await make_group(db, "Dup2", parent_id=corps.id, kind="brigade")
    post = await make_post(db, "DupPost", group_id=b1.id)
    await make_admin(db)
    x = await _admin_at(db, Node.group(b1.id))
    await _raw_assign(db, x, await _builtin(db, BuiltinRole.ADMINISTRATOR), Node.group(b2.id))
    maintainer = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, maintainer, await make_role(db, [P.POST_VIEW, P.POST_UPDATE]), Node.group(corps.id))
    url = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)
    moved = await client.put(url, json={"group_id": b2.id}, headers=headers(maintainer))
    assert moved.status_code == 200, moved.text

    await _admin_at(db, Node.group(b1.id))      # a second administrator, at the first brigade only
    _grant_refused(await client.put(url, json={"group_id": b1.id}, headers=headers(maintainer)), P.USER_UPDATE)


async def test_archived_users_and_archived_roles_do_not_count(client, db, headers):
    """Their assignments apply nowhere: restore_user and a reactivation run check_target_user at every node of the
    user, the restore of a role check_role_grant at every node of the role"""
    corps = await make_group(db, "CorpsGone", kind="corps")
    brigade = await make_group(db, "BrigadeGone", parent_id=corps.id, kind="brigade")
    group = await make_group(db, "GroupArchivedRole")
    post = await make_post(db, "Movable")
    await make_admin(db)
    gone = await _admin_at(db, Node.group(brigade.id))
    gone.is_deleted, gone.is_active = True, False
    db.add(gone)
    await db.commit()
    holder = await _user(db, UserType.ADMINISTRATOR)
    archived_role = await make_role(db, [P.USER_UPDATE, P.USER_VIEW, P.OBSERVATION_VIEW])
    await _raw_assign(db, holder, archived_role, Node.group(group.id))
    await _archive(db, archived_role)
    maintainer = await _user(db, UserType.ADMINISTRATOR)
    await _raw_assign(db, maintainer, await make_role(db, [P.POST_VIEW, P.POST_UPDATE, P.POST_ARCHIVE]), ROOT)

    url = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)
    under_gone = await client.put(url, json={"group_id": brigade.id}, headers=headers(maintainer))
    assert under_gone.status_code == 200, under_gone.text
    under_role = await client.put(url, json={"group_id": group.id}, headers=headers(maintainer))
    assert under_role.status_code == 200, under_role.text

    # The same for a restore: the administrator of an archived post is an archived user
    archived_post = await make_post(db, "ArchivedHolder")
    gone_holder = await _admin_at(db, Node.post(archived_post.id))
    gone_holder.is_deleted, gone_holder.is_active = True, False
    db.add(gone_holder)
    await _archive(db, archived_post)
    restored = await client.post(V1.path(PostRoutes.POST_RESTORE, post_id=archived_post.id),
                                 headers=headers(maintainer))
    assert restored.status_code == 200, restored.text


async def test_brigade_moved_through_scope_groups_is_checked(client, db, headers):
    corps_mine = await make_group(db, "CorpsMineSG", kind="corps")
    corps_theirs = await make_group(db, "CorpsTheirsSG", kind="corps")
    brigade = await make_group(db, "BrigadeSG", parent_id=corps_theirs.id, kind="brigade")
    await make_post(db, "InBrigadeSG", group_id=brigade.id)
    admin = await make_admin(db)
    mover = await _admin_at(db, Node.group(corps_mine.id))
    await _raw_assign(db, mover, await make_role(db, STRUCTURE), ROOT)
    url = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id)
    _grant_refused(await client.patch(url, json={"parent_id": corps_mine.id}, headers=headers(mover)), P.USER_UPDATE)
    assert await _group_parent(db, brigade.id) == corps_theirs.id
    moved = await client.patch(url, json={"parent_id": corps_mine.id}, headers=headers(admin))
    assert moved.status_code == 200, moved.text


async def test_plain_group_restore_by_the_administrator_above(client, db, headers):
    parent = await make_group(db, "ParentR")
    group = await make_group(db, "ChildR", parent_id=parent.id)
    await make_admin(db)
    await _admin_at(db, Node.group(group.id))
    await _archive(db, group)
    above = await _admin_at(db, Node.group(parent.id))
    restored = await client.post(V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=group.id), headers=headers(above))
    assert restored.status_code == 200, restored.text
