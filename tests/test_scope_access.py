"""Permission at the post (authorization v2, spec section 5): post records follow post.view, user records user.view,
counters observation.view, statistics statistics.view, the upload list media.manage_any; archived rows only where
X.archive is in effect; no administrator exception and no admin gate on these routes.

The legacy factories of tests/helpers.py build users the frozen conversion turns into grants at the root and at
posts. Grants at a group (and a permission at one post only) cannot be built from the old tables yet; the
`with_grants` fixture gives a user exactly the grants a test names, through the real request chain, until the
assignment API exists.
"""
import dataclasses

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth import dependencies
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree
from app.backend.enums.errors import AccessErrorCode
from app.backend.enums.general import Permissions as P
from app.backend.errors.problems import ApiError
from app.backend.enums.routes import MediaRoutes, PostRoutes, SearchRoutes, StatisticsRoutes, UserRoutes
from app.backend.filters.post_filter import PostCriteria, PostFilterService
from app.backend.filters.user_filter import UserCriteria, UserFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User
from app.backend.services.media import uploads
from tests.helpers import make_admin, make_dictionary, make_observation, make_post, make_user, perm_name

POSTS = V1.path(PostRoutes.ROOT)
USERS = V1.path(UserRoutes.ROOT)
SEARCH_POSTS = V1.path(SearchRoutes.POSTS)
SEARCH_USERS = V1.path(SearchRoutes.USERS)
STATISTICS = V1.path(StatisticsRoutes.ROOT)
UPLOADS = V1.path(MediaRoutes.UPLOADS)


def _post_url(post_id: int) -> str:
    return V1.path(PostRoutes.POST_BY_ID, post_id=post_id)


def _user_url(user_id: int) -> str:
    return V1.path(UserRoutes.USER_BY_ID, user_id=user_id)


def _stats_url(post_id: int) -> str:
    return V1.path(PostRoutes.POST_OBSERVATION_STATS, post_id=post_id)


def grant(node: Node, *perms) -> Grant:
    return Grant(0, "test_role", "Test role", False, node, frozenset(perm_name(p) for p in perms))


@pytest.fixture
def with_grants(monkeypatch):
    """with_grants(user, *grants): the AuthContext of every request of the user carries exactly these grants (the
    legacy fields stay as the old tables say; the routes of this module read only the grants)"""
    table = {}
    original = dependencies.load_auth_context

    async def patched(session, user, session_id=None):
        auth = await original(session, user, session_id=session_id)
        if user.id in table:
            auth = dataclasses.replace(auth, grants=table[user.id])
        return auth

    monkeypatch.setattr(dependencies, "load_auth_context", patched)

    def set_grants(user, *grants):
        table[user.id] = tuple(grants)
    return set_grants


async def _group(db, name: str, parent_id=None) -> ScopeGroup:
    group = ScopeGroup(name=name, parent_id=parent_id, parent_key=parent_id or 0)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def _archive(db, row) -> None:
    row.is_deleted = True
    db.add(row)
    await db.commit()


def _ids(response) -> set:
    assert response.status_code == 200, response.text
    body = response.json()
    return {item["id"] for item in (body["items"] if isinstance(body, dict) else body)}


def _denied(response, code: str, missing: list) -> dict:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == code, body
    assert body["missing"] == missing, body
    return body


# ----------------------------------------------------------------- post records
async def test_post_records_follow_post_view(client, db, headers):
    a, b = await make_post(db), await make_post(db)
    user = await make_user(db, perms=[P.POST_VIEW, P.OBSERVATION_VIEW], post_id=a.id)

    body = _denied(await client.get(_post_url(b.id), headers=headers(user)), "post_access_denied", ["post.view"])
    assert body["required"] == {"all": ["post.view"], "any": [], "admin": False}
    assert body["post_id"] == b.id and body["resource_type"] == "post"
    assert (await client.get(_post_url(a.id), headers=headers(user))).status_code == 200
    assert _ids(await client.get(POSTS, headers=headers(user))) == {a.id}
    assert _ids(await client.get(SEARCH_POSTS, headers=headers(user))) == {a.id}

    # admin.true with post.view: post.view at the root (the record administration the conversion keeps)
    manager = await make_user(db, perms=[P.IS_ADMIN, P.POST_VIEW], post_id=a.id)
    assert (await client.get(_post_url(b.id), headers=headers(manager))).status_code == 200
    assert {a.id, b.id} <= _ids(await client.get(POSTS, headers=headers(manager)))


async def test_observation_stats_follow_observation_view(client, db, headers):
    a, b = await make_post(db), await make_post(db)
    user = await make_user(db, perms=[P.POST_VIEW, P.OBSERVATION_VIEW], post_id=a.id)
    assert (await client.get(_stats_url(a.id), headers=headers(user))).status_code == 200
    _denied(await client.get(_stats_url(b.id), headers=headers(user)), "post_access_denied", ["observation.view"])

    # admin.true sees every post record, but counters are observation data: the home post only
    manager = await make_user(db, perms=[P.IS_ADMIN, P.POST_VIEW, P.OBSERVATION_VIEW], post_id=a.id)
    assert (await client.get(_post_url(b.id), headers=headers(manager))).status_code == 200
    _denied(await client.get(_stats_url(b.id), headers=headers(manager)), "post_access_denied", ["observation.view"])

    admin = await make_admin(db)
    assert (await client.get(_stats_url(b.id), headers=headers(admin))).status_code == 200
    assert (await client.get(_stats_url(999999), headers=headers(admin))).status_code == 404


async def test_archived_post_needs_post_archive_at_post(client, db, headers):
    post = await make_post(db)
    home = await make_user(db, perms=[P.POST_VIEW], post_id=post.id)
    viewer = await make_user(db, perms=[P.IS_ADMIN, P.POST_VIEW])
    archiver = await make_user(db, perms=[P.IS_ADMIN, P.POST_VIEW, P.POST_ARCHIVE])
    admin = await make_admin(db)
    await _archive(db, post)

    # a direct grant on an archived post stops applying: post.view is in effect at no post at all
    _denied(await client.get(_post_url(post.id), headers=headers(home)), "no_post_assigned", ["post.view"])
    # admin.true no longer shows archived records by itself: post.archive must be in effect at the post
    hidden = await client.get(_post_url(post.id), headers=headers(viewer))
    assert hidden.status_code == 404 and hidden.json()["code"] == "post_not_found", hidden.text
    for user in (archiver, admin):
        shown = await client.get(_post_url(post.id), headers=headers(user))
        assert shown.status_code == 200 and shown.json()["is_deleted"] is True, shown.text
        assert post.id in _ids(await client.get(POSTS, params={"include_deleted": "true"}, headers=headers(user)))


async def test_archived_posts_are_listed_only_where_post_archive_is_held(client, db, headers, with_grants):
    east = await _group(db, "East")
    in_east, outside = await make_post(db, group_id=east.id), await make_post(db)
    await _archive(db, in_east)
    await _archive(db, outside)
    manager = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE])
    with_grants(manager, grant(ROOT, P.POST_VIEW), grant(Node.group(east.id), P.POST_ARCHIVE))

    for path in (POSTS, SEARCH_POSTS):
        listed = _ids(await client.get(path, params={"include_deleted": "true"}, headers=headers(manager)))
        assert in_east.id in listed and outside.id not in listed
    assert (await client.get(_post_url(in_east.id), headers=headers(manager))).status_code == 200
    assert (await client.get(_post_url(outside.id), headers=headers(manager))).status_code == 404


async def test_removed_gate_does_not_widen(client, db, headers):
    post = await make_post(db)
    creator = await make_user(db, perms=[P.POST_VIEW, P.POST_CREATE], post_id=post.id)
    body = _denied(await client.post(POSTS, json={"name": "New", "code": "NEW-1"}, headers=headers(creator)),
                   "permission_denied", ["post.create"])
    assert body["required"] == {"all": ["post.view", "post.create"], "any": [], "admin": False}

    editor = await make_user(db, perms=[P.POST_VIEW, P.POST_UPDATE], post_id=post.id)
    _denied(await client.put(_post_url(post.id), json={"name": "x"}, headers=headers(editor)),
            "permission_denied", ["post.update"])
    archiver = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE, P.POST_ACCESS_ALL])
    _denied(await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(archiver)),
            "permission_denied", ["post.archive"])


async def test_create_and_move_need_the_permission_at_the_target(client, db, headers, with_grants):
    east, west = await _group(db, "East"), await _group(db, "West")
    sector = await _group(db, "Sector", parent_id=east.id)
    in_east, in_west = await make_post(db, group_id=east.id), await make_post(db, group_id=west.id)
    manager = await make_user(db, perms=[P.POST_VIEW, P.POST_CREATE, P.POST_UPDATE])
    with_grants(manager, grant(Node.group(east.id), P.POST_VIEW, P.POST_CREATE, P.POST_UPDATE))

    created = await client.post(POSTS, json={"name": "A", "code": "EAST-1", "group_id": sector.id}, headers=headers(manager))
    assert created.status_code == 200 and created.json()["group_id"] == sector.id, created.text
    for group_id, code in ((None, "ROOT-1"), (west.id, "WEST-1")):
        body = _denied(await client.post(POSTS, json={"name": "B", "code": code, "group_id": group_id}, headers=headers(manager)),
                       "permission_required_for_action", ["post.create"])
        assert body["permission"] == "post.create" and body["action"] == "create_post"
    assert (await db.exec(select(Post).where(Post.code.in_(["ROOT-1", "WEST-1"])))).all() == []

    moved = await client.put(_post_url(in_east.id), json={"group_id": sector.id}, headers=headers(manager))
    assert moved.status_code == 200 and moved.json()["group_id"] == sector.id, moved.text
    for group_id in (west.id, None):
        body = _denied(await client.put(_post_url(in_east.id), json={"group_id": group_id}, headers=headers(manager)),
                       "permission_required_for_action", ["post.update"])
        assert body["permission"] == "post.update" and body["action"] == "move_post"
    body = _denied(await client.put(_post_url(in_west.id), json={"name": "x"}, headers=headers(manager)),
                   "post_write_denied", ["post.update"])
    assert body["post_id"] == in_west.id
    await db.refresh(in_east)
    assert in_east.group_id == sector.id


async def test_archive_and_restore_follow_post_archive_at_the_post(client, db, headers, with_grants):
    east = await _group(db, "East")
    in_east, outside = await make_post(db, group_id=east.id), await make_post(db)
    direct = await make_post(db)
    manager = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE])
    with_grants(manager, grant(Node.group(east.id), P.POST_VIEW, P.POST_ARCHIVE),
                grant(Node.post(direct.id), P.POST_VIEW, P.POST_ARCHIVE))
    archive, restore = PostRoutes.POST_ARCHIVE, PostRoutes.POST_RESTORE

    assert (await client.post(V1.path(archive, post_id=in_east.id), headers=headers(manager))).status_code == 200
    assert (await client.post(V1.path(restore, post_id=in_east.id), headers=headers(manager))).status_code == 200
    _denied(await client.post(V1.path(archive, post_id=outside.id), headers=headers(manager)), "post_write_denied", ["post.archive"])
    # a grant on the post itself archives it, but stops applying once it is archived: restore needs an inherited grant
    assert (await client.post(V1.path(archive, post_id=direct.id), headers=headers(manager))).status_code == 200
    _denied(await client.post(V1.path(restore, post_id=direct.id), headers=headers(manager)), "post_write_denied", ["post.archive"])


# ----------------------------------------------------------------- user records
async def test_users_without_home_post_need_user_view_at_root(client, db, headers):
    a, b = await make_post(db), await make_post(db)
    colleague = await make_user(db, post_id=a.id)
    stranger = await make_user(db, post_id=b.id)
    homeless = await make_user(db)
    viewer = await make_user(db, perms=[P.USER_VIEW], post_id=a.id)
    # user.view through a read-only row at A, no home post: the caller is always in their own list
    reader = await make_user(db, perms=[P.USER_VIEW], extra_access={a.id: False})

    for path in (USERS, SEARCH_USERS):
        for caller in (viewer, reader):
            listed = _ids(await client.get(path, headers=headers(caller)))
            assert {caller.id, colleague.id} <= listed, (path, caller.username)
            assert homeless.id not in listed and stranger.id not in listed
    assert (await client.get(_user_url(reader.id), headers=headers(reader))).status_code == 200
    _denied(await client.get(_user_url(homeless.id), headers=headers(viewer)), "user_self_only", [])
    _denied(await client.get(USERS, params={"post_id": b.id}, headers=headers(viewer)), "post_access_denied", ["user.view"])

    admin = await make_admin(db)
    assert {colleague.id, stranger.id, homeless.id, viewer.id, reader.id} <= _ids(await client.get(USERS, headers=headers(admin)))
    assert (await client.get(_user_url(homeless.id), headers=headers(admin))).status_code == 200


async def test_archived_user_needs_user_archive_covering_them(client, db, headers):
    a = await make_post(db)
    gone = await make_user(db, post_id=a.id)
    await _archive(db, gone)
    viewer = await make_user(db, perms=[P.IS_ADMIN, P.USER_VIEW], post_id=a.id)
    archiver = await make_user(db, perms=[P.IS_ADMIN, P.USER_VIEW, P.USER_ARCHIVE], post_id=a.id)

    hidden = await client.get(_user_url(gone.id), headers=headers(viewer))
    assert hidden.status_code == 404 and hidden.json()["code"] == "user_not_found", hidden.text
    shown = await client.get(_user_url(gone.id), headers=headers(archiver))
    assert shown.status_code == 200 and shown.json()["is_deleted"] is True, shown.text


def _context(tree: ScopeTree, *grants: Grant) -> AuthContext:
    """A context with exactly these grants (no legacy permissions) for a caller that is not in the database"""
    caller = User(id=10**6, username="unit_caller", username_alias="Unit caller")
    return AuthContext(user=caller, permissions=frozenset(), roles=(), readable_post_ids=frozenset(),
                       writable_post_ids=frozenset(), grants=grants, tree=tree)


async def test_include_deleted_shows_archived_only_where_archive_is_held(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    live_a, gone_a = await make_user(db, post_id=a.id), await make_user(db, post_id=a.id)
    live_b, gone_b = await make_user(db, post_id=b.id), await make_user(db, post_id=b.id)
    gone_homeless = await make_user(db)
    for user in (gone_a, gone_b, gone_homeless):
        await _archive(db, user)
    tree = ScopeTree(group_parent={}, archived_groups=frozenset(), post_group={a.id: None, b.id: None},
                     archived_posts=frozenset())
    # user.view at A and B (B a read-only row), user.archive only at A
    grants = (grant(Node.post(a.id), P.USER_VIEW, P.USER_ARCHIVE), grant(Node.post(b.id), P.USER_VIEW))
    auth = _context(tree, *grants)

    # archived_scope: X.archive held anywhere, then the posts where it is in effect
    archived = AccessControlService.archived_scope(auth, UserCriteria(include_deleted=True), P.USER_ARCHIVE)
    assert archived == {a.id}
    assert AccessControlService.archived_scope(_context(tree, grant(ROOT, P.USER_ARCHIVE)),
                                               UserCriteria(include_deleted=True), P.USER_ARCHIVE) is None
    with pytest.raises(ApiError) as refused:
        AccessControlService.archived_scope(_context(tree, grants[1]), UserCriteria(include_deleted=True), P.USER_ARCHIVE)
    assert refused.value.code == AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION

    # the filter: archived rows only where post_column is in archived_post_ids, live rows untouched
    async def listed(criteria, archived_post_ids):
        query = UserFilterService.apply_filters(select(User), criteria, frozenset({a.id, b.id}),
                                                archived_post_ids=archived_post_ids)
        return {user.id for user in (await db.exec(query)).all()}

    assert await listed(UserCriteria(include_deleted=True), archived) == {live_a.id, gone_a.id, live_b.id}
    assert await listed(UserCriteria(include_deleted=True), None) == {live_a.id, gone_a.id, live_b.id, gone_b.id}
    assert await listed(UserCriteria(), archived) == {live_a.id, live_b.id}

    gone_post = await make_post(db)
    await _archive(db, gone_post)

    async def post_ids(criteria, archived_post_ids):
        query = PostFilterService._apply_soft_delete(select(Post.id), criteria, Post.id, archived_post_ids)
        return set((await db.exec(query)).all())

    assert await post_ids(PostCriteria(include_deleted=True), frozenset()) == {a.id, b.id}
    assert await post_ids(PostCriteria(include_deleted=True), frozenset({gone_post.id})) == {a.id, b.id, gone_post.id}
    assert await post_ids(PostCriteria(include_deleted=True), None) == {a.id, b.id, gone_post.id}
    assert await post_ids(PostCriteria(), None) == {a.id, b.id}

    # end to end, with the same grants
    caller = await make_user(db, perms=[P.USER_VIEW, P.USER_ARCHIVE])
    with_grants(caller, *grants)
    for path in (USERS, SEARCH_USERS):
        shown = _ids(await client.get(path, params={"include_deleted": "true"}, headers=headers(caller)))
        assert {live_a.id, gone_a.id, live_b.id} <= shown, path
        assert gone_b.id not in shown and gone_homeless.id not in shown

    # an administrator: user.archive at the root, every archived user
    admin = await make_admin(db)
    shown = _ids(await client.get(USERS, params={"include_deleted": "true"}, headers=headers(admin)))
    assert {gone_a.id, gone_b.id, gone_homeless.id} <= shown


# ----------------------------------------------------------------- aggregates and the upload list
async def test_statistics_follow_statistics_view(client, db, headers, with_grants):
    east = await _group(db, "East")
    in_east, other_east, outside = (await make_post(db, group_id=east.id), await make_post(db, group_id=east.id),
                                    await make_post(db))
    analyst = await make_user(db, perms=[P.STATISTICS_VIEW], post_id=outside.id)
    with_grants(analyst, grant(Node.group(east.id), P.STATISTICS_VIEW))
    body = (await client.get(STATISTICS, headers=headers(analyst))).json()
    assert body["post_ids"] == sorted([in_east.id, other_east.id])

    manager = await make_user(db, perms=[P.IS_ADMIN, P.STATISTICS_VIEW], post_id=outside.id)
    assert (await client.get(STATISTICS, headers=headers(manager))).json()["post_ids"] is None


async def test_upload_list_follows_manage_any_at_its_posts(client, db, headers):
    a, b, c = await make_post(db), await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    perms = [P.MEDIA_VIEW, P.MEDIA_CREATE]
    uploader_a = await make_user(db, perms=perms, post_id=a.id)
    uploader_b = await make_user(db, perms=perms, post_id=b.id)
    manager = await make_user(db, perms=[P.IS_ADMIN, *perms, P.MEDIA_MANAGE_ANY], post_id=a.id)

    async def opened(user, post):
        observation = await make_observation(db, user, post, ids["signal_type_id"])
        row = uploads.new_session(user.id, observation.id, post.id, "image", "a.png", 100, "image/png", None)
        db.add(row)
        await db.commit()
        return row.id

    at_a, at_b = await opened(uploader_a, a), await opened(uploader_b, b)
    # the manager's own session at a post outside their scope (opened before they lost the access)
    own_at_c = await opened(manager, c)

    # media.manage_any is in effect at A only: other users' sessions of A, plus their own anywhere
    assert _ids(await client.get(UPLOADS, headers=headers(manager))) == {at_a, own_at_c}
    assert _ids(await client.get(UPLOADS, headers=headers(uploader_a))) == {at_a}
    admin = await make_admin(db)
    assert _ids(await client.get(UPLOADS, headers=headers(admin))) == {at_a, at_b, own_at_c}
