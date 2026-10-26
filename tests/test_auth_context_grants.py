"""AuthContext: the grants of the user's role assignments on the scope tree. Users are built with the legacy-shaped
factory of tests/helpers.py (written as migration 023 converts them) or with real assignments (assign)."""
import pytest
from sqlmodel import select

from app.backend.auth.context import load_auth_context, load_scope_tree
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions as P
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.scope_group import ScopeGroup
from tests.helpers import ACCESS_ALL, ADMIN_TRUE, assign, make_admin, make_group, make_post, make_role, make_user


async def _archive_post(db, post):
    post.is_deleted = True
    db.add(post)
    await db.commit()
    await db.refresh(post)


async def _group(db, name, parent_id=None, archived=False) -> ScopeGroup:
    group = ScopeGroup(name=name, parent_id=parent_id, parent_key=parent_id or 0, is_deleted=archived)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def test_grants_match_legacy_scope(db):
    a, b, c = await make_post(db), await make_post(db), await make_post(db)
    user = await make_user(db, perms=[P.OBSERVATION_VIEW, P.OBSERVATION_UPDATE], post_id=a.id,
                           extra_access={b.id: True, c.id: False})
    auth = await load_auth_context(db, user)
    assert auth.posts_with(P.OBSERVATION_VIEW) == {a.id, b.id, c.id}
    assert auth.posts_with(P.OBSERVATION_UPDATE) == {a.id, b.id}
    assert auth.has_at_post(P.OBSERVATION_UPDATE, b.id) and not auth.has_at_post(P.OBSERVATION_UPDATE, c.id)
    assert auth.has_at_post("observation.view", c.id)
    assert auth.held_anywhere == {"observation.view", "observation.update"}
    assert auth.has_anywhere(P.OBSERVATION_UPDATE) and not auth.has_at_root(P.OBSERVATION_VIEW)
    assert {g.node for g in auth.grants} == {Node.post(a.id), Node.post(b.id), Node.post(c.id)}


async def test_admin_sees_every_post_record(db):
    admin = await make_admin(db)
    auth = await load_auth_context(db, admin)
    assert auth.posts_with(P.POST_VIEW) is None and auth.has_at_root(P.TAG_CREATE)
    tree, system = auth.grants
    administrator = (await db.exec(select(Role).where(Role.builtin_key == "administrator"))).one()
    system_administrator = (await db.exec(select(Role).where(Role.builtin_key == "system_administrator"))).one()
    assert tree.node == ROOT and tree.role_is_builtin and tree.role_id == administrator.id and tree.kind == "tree"
    assert system.node == ROOT and system.role_id == system_administrator.id and system.kind == "system"
    for grant in auth.grants:
        assert "admin.true" not in grant.permissions and "post.access_all" not in grant.permissions
    assert auth.covers(P.USER_UPDATE, ROOT) and auth.covers_user(P.USER_UPDATE, admin)
    assert auth.root_permissions | auth.system_permissions == auth.held_anywhere and auth.post_permissions() == {}
    assert not auth.root_permissions & auth.system_permissions
    assert not hasattr(auth, "is_admin") and not hasattr(auth, "roles")   # the administrator: privileges.holds_admin_at_root


async def test_admin_true_record_exception_and_post_scope(db):
    a, b = await make_post(db), await make_post(db)
    user = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW, P.USER_VIEW, P.OBSERVATION_VIEW], post_id=a.id)
    auth = await load_auth_context(db, user)
    assert auth.posts_with(P.POST_VIEW) is None              # post records: every post, like post_record_scope
    assert auth.posts_with(P.OBSERVATION_VIEW) == {a.id}     # data: the home post only
    assert not auth.has_at_post(P.OBSERVATION_VIEW, b.id)
    assert "admin.true" not in auth.held_anywhere


async def test_access_all_is_every_post(db):
    a = await make_post(db)
    user = await make_user(db, perms=[ACCESS_ALL, P.OBSERVATION_VIEW, P.OBSERVATION_CREATE])
    auth = await load_auth_context(db, user)
    assert auth.posts_with(P.OBSERVATION_VIEW) is None and auth.posts_with(P.OBSERVATION_CREATE) is None
    assert auth.has_at_post(P.OBSERVATION_CREATE, a.id)
    # every id, even one no post has (the route answers 404 after the scope check)
    assert auth.has_at_post(P.OBSERVATION_VIEW, 0) is True


async def test_unknown_post_ids_never_fail(db):
    a = await make_post(db)
    user = await make_user(db, perms=[P.OBSERVATION_VIEW], post_id=a.id)
    auth = await load_auth_context(db, user)
    for post_id in (0, -1, a.id + 1000):
        assert auth.has_at_post(P.OBSERVATION_VIEW, post_id) is False


async def test_dead_permissions_are_held_nowhere(db):
    a = await make_post(db)
    user = await make_user(db, perms=[P.OBSERVATION_VIEW, P.TAG_CREATE, P.USER_UPDATE], post_id=a.id)
    auth = await load_auth_context(db, user)
    assert auth.permissions == {"observation.view"}
    assert not auth.has_anywhere(P.TAG_CREATE) and not auth.has_anywhere(P.USER_UPDATE)
    assert auth.posts_with(P.USER_UPDATE) == frozenset()


async def test_dictionary_reader_without_post_holds_views_at_root(db):
    user = await make_user(db, perms=[P.CATEGORY_VIEW, P.TAG_VIEW, P.OBSERVATION_VIEW])
    auth = await load_auth_context(db, user)
    assert auth.has_at_root(P.CATEGORY_VIEW) and auth.has_at_root(P.TAG_VIEW)
    assert not auth.has_anywhere(P.OBSERVATION_VIEW)
    assert auth.posts_with(P.OBSERVATION_VIEW) == frozenset()


async def test_archived_post_leaves_the_direct_scope(db):
    a, b = await make_post(db), await make_post(db)
    user = await make_user(db, perms=[P.OBSERVATION_VIEW, P.OBSERVATION_UPDATE], post_id=a.id,
                           extra_access={b.id: False})
    await _archive_post(db, b)
    auth = await load_auth_context(db, user)
    assert auth.posts_with(P.OBSERVATION_VIEW) == {a.id}
    assert not auth.has_at_post(P.OBSERVATION_VIEW, b.id)
    assert b.id in auth.tree.archived_posts
    # the grant stays (applies again after a restore) and the permission is still held
    assert Node.post(b.id) in {g.node for g in auth.grants} and auth.has_anywhere(P.OBSERVATION_VIEW)


async def test_covers_user_follows_the_home_post(db):
    a, b = await make_post(db), await make_post(db)
    caller = await make_user(db, perms=[P.USER_VIEW], post_id=a.id)
    same_post, other_post, no_post = (await make_user(db, post_id=a.id), await make_user(db, post_id=b.id),
                                      await make_user(db))
    auth = await load_auth_context(db, caller)
    assert auth.covers_user(P.USER_VIEW, same_post)
    assert not auth.covers_user(P.USER_VIEW, other_post)
    assert not auth.covers_user(P.USER_VIEW, no_post)          # a user without a home post needs the root
    assert auth.covers(P.USER_VIEW, Node.post(a.id)) and not auth.covers(P.USER_VIEW, ROOT)


async def test_archived_role_gives_no_grant(db):
    a = await make_post(db)
    role = await make_role(db, [P.OBSERVATION_VIEW])
    user = await make_user(db, role=role, post_id=a.id)
    role.is_deleted = True
    db.add(role)
    await db.commit()
    auth = await load_auth_context(db, user)
    assert auth.grants == () and auth.held_anywhere == frozenset() and auth.permissions == frozenset()


async def test_load_scope_tree(db):
    east = await _group(db, "East")
    kharkiv = await _group(db, "Kharkiv", parent_id=east.id)
    gone = await _group(db, "Gone", archived=True)
    seven = await make_post(db, group_id=kharkiv.id)
    root_post = await make_post(db)
    archived = await make_post(db, group_id=gone.id)
    await _archive_post(db, archived)
    tree = await load_scope_tree(db)
    assert tree.group_parent[kharkiv.id] == east.id and tree.group_parent[east.id] is None
    assert tree.archived_groups == {gone.id}
    assert tree.post_group[seven.id] == kharkiv.id and tree.post_group[root_post.id] is None
    assert archived.id in tree.archived_posts and seven.id not in tree.archived_posts
    assert tree.posts_under(Node.group(east.id)) == {seven.id}

    user = await make_user(db, perms=[P.OBSERVATION_VIEW], post_id=seven.id)
    auth = await load_auth_context(db, user)
    assert auth.tree == tree


@pytest.mark.fit_types
async def test_grants_come_from_role_assignments(db):
    """One grant per assignment of a role that is not archived, at the node of its scope columns"""
    east = await make_group(db, "East")
    a, b, outside = await make_post(db, group_id=east.id), await make_post(db, group_id=east.id), await make_post(db)
    viewer, editor = await make_role(db, [P.OBSERVATION_VIEW]), await make_role(db, [P.OBSERVATION_VIEW, P.OBSERVATION_UPDATE])
    user = await make_user(db)
    at_group = await assign(db, user, viewer, Node.group(east.id))
    at_post = await assign(db, user, editor, Node.post(outside.id))
    at_root = await assign(db, user, viewer, ROOT)

    auth = await load_auth_context(db, user)
    assert [(g.assignment_id, g.node) for g in auth.grants] == [
        (at_group.id, Node.group(east.id)), (at_post.id, Node.post(outside.id)), (at_root.id, ROOT)]
    assert sorted({g.role_id for g in auth.grants}) == sorted([viewer.id, editor.id])
    assert auth.root_permissions == {"observation.view"}
    # Below the root: the group's posts and the post of the editor grant; in effect = root_permissions + the entry
    assert auth.post_permissions() == {a.id: {"observation.view"}, b.id: {"observation.view"},
                                       outside.id: {"observation.view", "observation.update"}}
    assert auth.permissions_at_post(outside.id) == {"observation.view", "observation.update"}
    assert auth.permissions_at_post(a.id) == {"observation.view"}
    assert auth.posts_with(P.OBSERVATION_UPDATE) == {outside.id}
    assert auth.posts_with(P.OBSERVATION_VIEW) is None

    await db.delete(at_root)
    await db.commit()
    auth = await load_auth_context(db, user)
    assert auth.posts_with(P.OBSERVATION_VIEW) == {a.id, b.id, outside.id}
    assert auth.covers(P.OBSERVATION_VIEW, Node.post(a.id)) and not auth.covers(P.OBSERVATION_VIEW, ROOT)
    assert (await db.exec(select(RoleAssignment.scope_key).where(RoleAssignment.id == at_group.id))).one() == f"g:{east.id}"


async def test_grants_at_archived_nodes_are_held_nowhere(db):
    """R39 (spec section 5): a grant on an archived post or at an archived group applies nowhere, so it does not count
    as held anywhere either (require(), /auth/me permissions, role edits); a live group without posts still does"""
    gone_post, live = await make_post(db), await make_post(db)
    gone_group, empty_group = await _group(db, "Gone"), await _group(db, "Empty")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [P.POST_VIEW]), Node.post(gone_post.id))
    await assign(db, user, await make_role(db, [P.TAG_VIEW]), Node.group(gone_group.id))
    await assign(db, user, await make_role(db, [P.MEDIA_VIEW]), Node.group(empty_group.id))
    await assign(db, user, await make_role(db, [P.OBSERVATION_VIEW]), Node.post(live.id))
    await _archive_post(db, gone_post)
    gone_group.is_deleted = True
    db.add(gone_group)
    await db.commit()

    auth = await load_auth_context(db, user)
    assert len(auth.grants) == 4   # the grants stay: they apply again after a restore
    assert auth.held_anywhere == auth.permissions == {"media.view", "observation.view"}
    assert not auth.has_anywhere(P.POST_VIEW) and not auth.has_anywhere(P.TAG_VIEW)
    assert auth.posts_with(P.MEDIA_VIEW) == frozenset()   # held, at no post: check_post answers no_post_assigned
