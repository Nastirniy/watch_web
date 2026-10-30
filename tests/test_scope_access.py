"""Permission at the post (authorization v2, spec section 5): post records follow post.view, user records user.view,
counters observation.view, statistics statistics.view, the upload list media.manage_any; archived rows only where
X.archive is in effect; no administrator exception and no admin gate on these routes.

The legacy-shaped factory of tests/helpers.py writes users as migration 023 converts them (assignments at the root
and at posts). Grants at a group, or a permission at one post only, come from the `with_grants` fixture: it replaces
the user's role assignments with one role per named grant, at its node.
"""
import io

import openpyxl
import pytest
from sqlmodel import delete, select

from app.backend.api.v1 import V1
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree
from app.backend.enums.errors import AccessErrorCode
from app.backend.enums.general import Permissions as P
from app.backend.errors.problems import ApiError
from app.backend.config import settings
from app.backend.enums.routes import (
    AuthRoutes, MediaRoutes, ObservationRoutes, PostRoutes, RoleRoutes, ScopeGroupRoutes, SearchRoutes, StatisticsRoutes,
    TagRoutes, UserRoutes,
)
from app.backend.filters.post_filter import PostCriteria, PostFilterService
from app.backend.filters.user_filter import UserCriteria, UserFilterService
from app.backend.models.observation import ObservationFrequency
from app.backend.models.post import Post
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User
from app.backend.services.media import uploads
from tests.helpers import (
    ACCESS_ALL, ADMIN_TRUE, assign, give, make_admin, make_dictionary, make_media, make_observation, make_post, make_role,
    make_user, observation_payload, perm_name,
)

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
def with_grants(db):
    """await with_grants(user, *grants): the user's role assignments become exactly these, one new role holding the
    grant's permissions at the grant's node each"""
    async def set_grants(user, *grants):
        await db.exec(delete(RoleAssignment).where(RoleAssignment.user_id == user.id))
        await db.exec(delete(SystemRoleAssignment).where(SystemRoleAssignment.user_id == user.id))
        await db.commit()
        for granted in grants:
            await give(db, user, granted.permissions, granted.node)
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
    assert body["required"] == {"all": ["post.view"], "any": [], "at_root": []}
    assert body["post_id"] == b.id and body["resource_type"] == "post"
    assert (await client.get(_post_url(a.id), headers=headers(user))).status_code == 200
    assert _ids(await client.get(POSTS, headers=headers(user))) == {a.id}
    assert _ids(await client.get(SEARCH_POSTS, headers=headers(user))) == {a.id}

    # admin.true with post.view: post.view at the root (the record administration the conversion keeps)
    manager = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW], post_id=a.id)
    assert (await client.get(_post_url(b.id), headers=headers(manager))).status_code == 200
    assert {a.id, b.id} <= _ids(await client.get(POSTS, headers=headers(manager)))


async def test_observation_stats_follow_post_view_and_observation_view(client, db, headers):
    a, b = await make_post(db), await make_post(db)
    user = await make_user(db, perms=[P.POST_VIEW, P.OBSERVATION_VIEW], post_id=a.id)
    assert (await client.get(_stats_url(a.id), headers=headers(user))).status_code == 200
    _denied(await client.get(_stats_url(b.id), headers=headers(user)), "post_access_denied", ["post.view"])

    # admin.true sees every post record, but counters are observation data: the home post only
    manager = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW, P.OBSERVATION_VIEW], post_id=a.id)
    assert (await client.get(_post_url(b.id), headers=headers(manager))).status_code == 200
    _denied(await client.get(_stats_url(b.id), headers=headers(manager)), "post_access_denied", ["observation.view"])

    admin = await make_admin(db)
    assert (await client.get(_stats_url(b.id), headers=headers(admin))).status_code == 200
    assert (await client.get(_stats_url(999999), headers=headers(admin))).status_code == 404


async def test_archived_post_needs_post_archive_at_post(client, db, headers):
    post = await make_post(db)
    home = await make_user(db, perms=[P.POST_VIEW], post_id=post.id)
    viewer = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW])
    archiver = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW, P.POST_ARCHIVE])
    admin = await make_admin(db)
    await _archive(db, post)

    # a direct grant on an archived post stops applying: post.view is held nowhere (R39; before: no_post_assigned)
    _denied(await client.get(_post_url(post.id), headers=headers(home)), "permission_denied", ["post.view"])
    # admin.true no longer shows archived records by itself: post.archive must be in effect at the post
    hidden = await client.get(_post_url(post.id), headers=headers(viewer))
    assert hidden.status_code == 404 and hidden.json()["code"] == "post_not_found", hidden.text
    for user in (archiver, admin):
        shown = await client.get(_post_url(post.id), headers=headers(user))
        assert shown.status_code == 200 and shown.json()["is_deleted"] is True, shown.text
        assert post.id in _ids(await client.get(POSTS, params={"include_deleted": "true"}, headers=headers(user)))


async def test_a_grant_only_at_an_archived_node_is_held_nowhere(client, db, headers, with_grants):
    """R39: a permission whose only grant is on an archived post (or at an archived group) applies nowhere: it is not
    in /auth/me permissions and a route requiring it answers 403 permission_denied (before: no_post_assigned, or the
    route ran with an empty scope)"""
    post, other = await make_post(db), await make_post(db)
    gone = await _group(db, "Gone")
    user = await make_user(db, perms=[P.POST_VIEW], post_id=post.id)
    await with_grants(user, grant(Node.post(post.id), P.POST_VIEW), grant(Node.group(gone.id), P.OBSERVATION_VIEW),
                      grant(Node.post(other.id), P.TAG_VIEW))
    await _archive(db, post)
    await _archive(db, gone)

    me = (await client.get(V1.path(AuthRoutes.ME), headers=headers(user))).json()
    assert me["permissions"] == ["tag.view"] and me["post_permissions"] == {str(other.id): ["tag.view"]}
    body = _denied(await client.get(_post_url(other.id), headers=headers(user)), "permission_denied", ["post.view"])
    assert body["required"] == {"all": ["post.view"], "any": [], "at_root": []}
    _denied(await client.get(V1.path(ObservationRoutes.LIST_ALL), headers=headers(user)), "permission_denied",
            ["observation.view"])


@pytest.mark.fit_types
async def test_archived_posts_are_listed_only_where_post_archive_is_held(client, db, headers, with_grants):
    east = await _group(db, "East")
    in_east, outside = await make_post(db, group_id=east.id), await make_post(db)
    await _archive(db, in_east)
    await _archive(db, outside)
    manager = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE])
    await with_grants(manager, grant(ROOT, P.POST_VIEW), grant(Node.group(east.id), P.POST_ARCHIVE))

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
    assert body["required"] == {"all": ["post.view", "post.create"], "any": [], "at_root": []}

    editor = await make_user(db, perms=[P.POST_VIEW, P.POST_UPDATE], post_id=post.id)
    _denied(await client.put(_post_url(post.id), json={"name": "x"}, headers=headers(editor)),
            "permission_denied", ["post.update"])
    archiver = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE, ACCESS_ALL])
    _denied(await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(archiver)),
            "permission_denied", ["post.archive"])


@pytest.mark.fit_types
async def test_create_and_move_need_the_permission_at_the_target(client, db, headers, with_grants):
    east, west = await _group(db, "East"), await _group(db, "West")
    sector = await _group(db, "Sector", parent_id=east.id)
    in_east, in_west = await make_post(db, group_id=east.id), await make_post(db, group_id=west.id)
    manager = await make_user(db, perms=[P.POST_VIEW, P.POST_CREATE, P.POST_UPDATE])
    await with_grants(manager, grant(Node.group(east.id), P.POST_VIEW, P.POST_CREATE, P.POST_UPDATE))

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
    # a post the caller cannot see answers as GET does (post.view first), before the write permission
    body = _denied(await client.put(_post_url(in_west.id), json={"name": "x"}, headers=headers(manager)),
                   "post_access_denied", ["post.view"])
    assert body["post_id"] == in_west.id
    await db.refresh(in_east)
    assert in_east.group_id == sector.id


@pytest.mark.fit_types
async def test_a_move_needs_post_update_covering_the_old_and_the_new_location(client, db, headers, with_grants):
    east, west = await _group(db, "East"), await _group(db, "West")
    post = await make_post(db, group_id=east.id)
    puller = await make_user(db, perms=[P.POST_VIEW, P.POST_UPDATE])
    # post.update on the post itself and at West: in effect at the post, but East is not covered
    await with_grants(puller, grant(Node.post(post.id), P.POST_VIEW, P.POST_UPDATE),
                grant(Node.group(west.id), P.POST_VIEW, P.POST_UPDATE))
    body = _denied(await client.put(_post_url(post.id), json={"group_id": west.id}, headers=headers(puller)),
                   "permission_required_for_action", ["post.update"])
    assert body["permission"] == "post.update" and body["action"] == "move_post"
    await db.refresh(post)
    assert post.group_id == east.id
    # other fields of the post stay editable through the grant on the post
    assert (await client.put(_post_url(post.id), json={"name": "Renamed"}, headers=headers(puller))).status_code == 200

    mover = await make_user(db, perms=[P.POST_VIEW, P.POST_UPDATE])
    await with_grants(mover, grant(Node.group(east.id), P.POST_VIEW, P.POST_UPDATE),
                grant(Node.group(west.id), P.POST_VIEW, P.POST_UPDATE))
    moved = await client.put(_post_url(post.id), json={"group_id": west.id}, headers=headers(mover))
    assert moved.status_code == 200 and moved.json()["group_id"] == west.id, moved.text
    await db.refresh(post)
    assert post.group_id == west.id


@pytest.mark.fit_types
async def test_archive_and_restore_follow_post_archive_at_the_post(client, db, headers, with_grants):
    east = await _group(db, "East")
    in_east, outside = await make_post(db, group_id=east.id), await make_post(db)
    direct = await make_post(db)
    manager = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE])
    await with_grants(manager, grant(Node.group(east.id), P.POST_VIEW, P.POST_ARCHIVE),
                grant(Node.post(direct.id), P.POST_VIEW, P.POST_ARCHIVE))
    archive, restore = PostRoutes.POST_ARCHIVE, PostRoutes.POST_RESTORE

    assert (await client.post(V1.path(archive, post_id=in_east.id), headers=headers(manager))).status_code == 200
    assert (await client.post(V1.path(restore, post_id=in_east.id), headers=headers(manager))).status_code == 200
    _denied(await client.post(V1.path(archive, post_id=outside.id), headers=headers(manager)), "post_access_denied", ["post.view"])
    # a grant on the post itself archives it, but stops applying once it is archived: restore needs an inherited grant
    assert (await client.post(V1.path(archive, post_id=direct.id), headers=headers(manager))).status_code == 200
    _denied(await client.post(V1.path(restore, post_id=direct.id), headers=headers(manager)), "post_access_denied", ["post.view"])


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
    viewer = await make_user(db, perms=[ADMIN_TRUE, P.USER_VIEW], post_id=a.id)
    archiver = await make_user(db, perms=[ADMIN_TRUE, P.USER_VIEW, P.USER_ARCHIVE], post_id=a.id)

    hidden = await client.get(_user_url(gone.id), headers=headers(viewer))
    assert hidden.status_code == 404 and hidden.json()["code"] == "user_not_found", hidden.text
    shown = await client.get(_user_url(gone.id), headers=headers(archiver))
    assert shown.status_code == 200 and shown.json()["is_deleted"] is True, shown.text


def _context(tree: ScopeTree, *grants: Grant) -> AuthContext:
    """A context with exactly these grants for a caller that is not in the database"""
    caller = User(id=10**6, username="unit_caller", username_alias="Unit caller", user_type="viewer")
    return AuthContext(user=caller, grants=grants, tree=tree)


@pytest.mark.fit_types
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
                                                archived_post_ids=archived_post_ids, unit_nodes=None, assigned_nodes=None)
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
    await with_grants(caller, *grants)
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
    await with_grants(analyst, grant(Node.group(east.id), P.STATISTICS_VIEW))
    body = (await client.get(STATISTICS, headers=headers(analyst))).json()
    assert body["post_ids"] == sorted([in_east.id, other_east.id])

    manager = await make_user(db, perms=[ADMIN_TRUE, P.STATISTICS_VIEW], post_id=outside.id)
    assert (await client.get(STATISTICS, headers=headers(manager))).json()["post_ids"] is None


async def test_upload_list_follows_manage_any_at_its_posts(client, db, headers):
    a, b, c = await make_post(db), await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    perms = [P.MEDIA_VIEW, P.MEDIA_CREATE]
    uploader_a = await make_user(db, perms=perms, post_id=a.id)
    uploader_b = await make_user(db, perms=perms, post_id=b.id)
    manager = await make_user(db, perms=[ADMIN_TRUE, *perms, P.MEDIA_MANAGE_ANY], post_id=a.id)

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


# ================================================================= remaining routes, no admin gate anywhere
REPORT = V1.path(ObservationRoutes.REPORT)
ALL_FIELDS = {"report_type": "all_fields"}
OBSERVATIONS = V1.path(ObservationRoutes.LIST_ALL)
SEARCH_OBSERVATIONS = V1.path(SearchRoutes.OBSERVATIONS)
MEDIA = V1.path(MediaRoutes.ROOT)
SEARCH_MEDIA = V1.path(SearchRoutes.MEDIA)
MONITOR_ROUTES = (V1.path(MediaRoutes.STORAGE_STATISTICS), V1.path(MediaRoutes.POSTERS_STATUS))


def _observation_url(route, observation_id: int) -> str:
    return V1.path(route, observation_id=observation_id)


async def _observation(db, user, post, signal_type_id, comment=None, archived=False):
    observation = await make_observation(db, user, post, signal_type_id)
    observation.comment = comment
    observation.is_deleted = archived
    db.add(observation)
    await db.commit()
    await db.refresh(observation)
    return observation


def _report_texts(response) -> set:
    assert response.status_code == 200, response.text
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    return {cell.value for row in sheet.iter_rows() for cell in row if isinstance(cell.value, str)}


async def test_export_without_gate(client, db, headers):
    post = await make_post(db)
    exporter = await make_user(db, perms=[P.OBSERVATION_VIEW, P.EXPORT_CREATE], post_id=post.id)
    # export.create did nothing without admin.true: still refused, now as a missing permission
    body = _denied(await client.post(REPORT, json=ALL_FIELDS, headers=headers(exporter)), "permission_denied", ["export.create"])
    assert body["required"] == {"all": ["observation.view", "export.create"], "any": [], "at_root": []}
    admin = await make_admin(db)
    assert (await client.post(REPORT, json=ALL_FIELDS, headers=headers(admin))).status_code == 200
    # admin.true with both permissions keeps the export (the conversion puts export.create where observation.view is)
    manager = await make_user(db, perms=[ADMIN_TRUE, P.OBSERVATION_VIEW, P.EXPORT_CREATE], post_id=post.id)
    assert (await client.post(REPORT, json=ALL_FIELDS, headers=headers(manager))).status_code == 200


async def test_export_covers_observation_view_and_export_create(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    await _observation(db, owner, a, ids["signal_type_id"], comment="seen at A")
    await _observation(db, owner, b, ids["signal_type_id"], comment="seen at B")
    exporter = await make_user(db, perms=[ADMIN_TRUE, P.OBSERVATION_VIEW, P.EXPORT_CREATE], post_id=a.id,
                               extra_access={b.id: False})
    await with_grants(exporter, grant(Node.post(a.id), P.OBSERVATION_VIEW, P.EXPORT_CREATE),
                grant(Node.post(b.id), P.OBSERVATION_VIEW))

    texts = _report_texts(await client.post(REPORT, json=ALL_FIELDS, headers=headers(exporter)))
    assert "seen at A" in texts and "seen at B" not in texts
    body = _denied(await client.post(REPORT, json={**ALL_FIELDS, "post_ids": [b.id]}, headers=headers(exporter)),
                   "post_write_denied", ["export.create"])
    assert body["post_id"] == b.id


async def test_export_includes_archived_rows_only_where_observation_archive_is_held(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    await _observation(db, owner, a, ids["signal_type_id"], comment="archived at A", archived=True)
    await _observation(db, owner, b, ids["signal_type_id"], comment="archived at B", archived=True)
    await _observation(db, owner, b, ids["signal_type_id"], comment="live at B")
    perms = [P.OBSERVATION_VIEW, P.EXPORT_CREATE]
    exporter = await make_user(db, perms=[ADMIN_TRUE, *perms, P.OBSERVATION_ARCHIVE], post_id=a.id,
                               extra_access={b.id: True})
    await with_grants(exporter, grant(Node.post(a.id), *perms, P.OBSERVATION_ARCHIVE), grant(Node.post(b.id), *perms))

    texts = _report_texts(await client.post(REPORT, json={**ALL_FIELDS, "include_deleted": True}, headers=headers(exporter)))
    assert {"archived at A", "live at B"} <= texts and "archived at B" not in texts


async def test_media_monitor_route(client, db, headers):
    post = await make_post(db)
    viewer = await make_user(db, perms=[P.MEDIA_VIEW], post_id=post.id)
    admin = await make_admin(db)
    # storage monitoring was media.view behind the admin gate: an admin.true holder with media.view keeps it
    manager = await make_user(db, perms=[ADMIN_TRUE, P.MEDIA_VIEW], post_id=post.id)
    for path in MONITOR_ROUTES:
        body = _denied(await client.get(path, headers=headers(viewer)), "permission_denied", ["media.monitor"])
        assert body["required"] == {"all": ["media.monitor"], "any": [], "at_root": []}
        assert (await client.get(path, headers=headers(admin))).status_code == 200, path
        assert (await client.get(path, headers=headers(manager))).status_code == 200, path


async def test_poster_reconcile_needs_every_permission_at_the_root(client, db, headers, with_grants, tmp_path, monkeypatch):
    """R16: reconcile acts on every post, so media.view + media.update (scoped) are not enough, with or without
    media.monitor (finding L8): all three in effect at the root"""
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    reconcile = V1.path(MediaRoutes.POSTERS_RECONCILE)
    east = await _group(db, "East")
    post = await make_post(db, group_id=east.id)
    perms = [P.MEDIA_VIEW, P.MEDIA_UPDATE]
    post_admin = await make_user(db, perms=[ADMIN_TRUE, *perms], post_id=post.id)
    await with_grants(post_admin, grant(Node.group(east.id), *perms))
    body = _denied(await client.post(reconcile, json={}, headers=headers(post_admin)), "permission_denied",
                   ["media.view", "media.update", "media.monitor"])
    assert body["required"] == {"all": [], "any": [], "at_root": ["media.view", "media.update", "media.monitor"]}

    assert (await client.post(reconcile, json={}, headers=headers(await make_admin(db)))).status_code == 200
    # a converted administrator with media.view holds media.monitor at the root (R10) but the rest at the post only
    manager = await make_user(db, perms=[ADMIN_TRUE, *perms], post_id=post.id)
    _denied(await client.post(reconcile, json={}, headers=headers(manager)), "permission_denied",
            ["media.view", "media.update"])


async def test_manage_any_applies_at_its_posts_only(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=[P.OBSERVATION_VIEW, P.OBSERVATION_CREATE], post_id=a.id, extra_access={b.id: True})
    at_a = await _observation(db, owner, a, ids["signal_type_id"])
    at_b = await _observation(db, owner, b, ids["signal_type_id"])
    manager = await make_user(db, perms=[P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE, P.OBSERVATION_MANAGE_ANY],
                              post_id=a.id, extra_access={b.id: True})
    await with_grants(manager, grant(Node.post(a.id), P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE, P.OBSERVATION_MANAGE_ANY),
                grant(Node.post(b.id), P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE))

    _denied(await client.post(_observation_url(ObservationRoutes.OBSERVATION_ARCHIVE, at_b.id), headers=headers(manager)),
            "observation_not_owner", ["observation.manage_any"])
    archived = await client.post(_observation_url(ObservationRoutes.OBSERVATION_ARCHIVE, at_a.id), headers=headers(manager))
    assert archived.status_code == 200, archived.text


async def test_media_manage_any_applies_at_its_posts_only(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=[P.MEDIA_VIEW, P.MEDIA_CREATE], post_id=a.id, extra_access={b.id: True})
    media_a = await make_media(db, await _observation(db, owner, a, ids["signal_type_id"]), owner)
    media_b = await make_media(db, await _observation(db, owner, b, ids["signal_type_id"]), owner)
    manager = await make_user(db, perms=[P.MEDIA_VIEW, P.MEDIA_ARCHIVE, P.MEDIA_MANAGE_ANY], post_id=a.id,
                              extra_access={b.id: True})
    await with_grants(manager, grant(Node.post(a.id), P.MEDIA_VIEW, P.MEDIA_ARCHIVE, P.MEDIA_MANAGE_ANY),
                grant(Node.post(b.id), P.MEDIA_VIEW, P.MEDIA_ARCHIVE))

    _denied(await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media_b.id), headers=headers(manager)),
            "media_not_owner", ["media.manage_any"])
    archived = await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media_a.id), headers=headers(manager))
    assert archived.status_code == 200, archived.text


async def test_upload_session_of_another_user_needs_manage_any_at_its_post(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    perms = [P.MEDIA_VIEW, P.MEDIA_CREATE]
    uploader = await make_user(db, perms=perms, post_id=a.id, extra_access={b.id: True})
    sessions = {}
    for post in (a, b):
        observation = await _observation(db, uploader, post, ids["signal_type_id"])
        row = uploads.new_session(uploader.id, observation.id, post.id, "image", "a.png", 100, "image/png", None)
        db.add(row)
        await db.commit()
        sessions[post.id] = row.id
    manager = await make_user(db, perms=[*perms, P.MEDIA_MANAGE_ANY], post_id=a.id, extra_access={b.id: True})
    await with_grants(manager, grant(Node.post(a.id), *perms, P.MEDIA_MANAGE_ANY), grant(Node.post(b.id), *perms))

    _denied(await client.get(V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=sessions[b.id]), headers=headers(manager)),
            "upload_session_not_owner", ["media.manage_any"])
    shown = await client.get(V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=sessions[a.id]), headers=headers(manager))
    assert shown.status_code == 200, shown.text


async def test_observation_writes_need_their_permission_at_the_post(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    perms = [P.OBSERVATION_VIEW, P.OBSERVATION_CREATE, P.OBSERVATION_UPDATE]
    editor = await make_user(db, perms=perms, post_id=a.id, extra_access={b.id: True})
    await with_grants(editor, grant(Node.post(a.id), *perms), grant(Node.post(b.id), P.OBSERVATION_VIEW))
    at_a = await _observation(db, editor, a, ids["signal_type_id"])
    at_b = await _observation(db, editor, b, ids["signal_type_id"])

    url_b = _observation_url(ObservationRoutes.OBSERVATION_BY_ID, at_b.id)
    _denied(await client.put(url_b, json={"comment": "x"}, headers=headers(editor)), "post_write_denied", ["observation.update"])
    _denied(await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(b.id, ids["signal_type_id"]),
                              headers=headers(editor)), "post_write_denied", ["observation.create"])
    url_a = _observation_url(ObservationRoutes.OBSERVATION_BY_ID, at_a.id)
    assert (await client.put(url_a, json={"comment": "x"}, headers=headers(editor))).status_code == 200
    # a post where observation.view is not in effect at all: no access to its data
    c = await make_post(db)
    _denied(await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(c.id, ids["signal_type_id"]),
                              headers=headers(editor)), "post_access_denied", ["observation.view"])


@pytest.mark.fit_types
async def test_observations_of_an_archived_post_need_post_archive_at_it(client, db, headers, with_grants):
    post = await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    observation = await _observation(db, owner, post, ids["signal_type_id"])
    await _archive(db, post)
    perms = [ADMIN_TRUE, ACCESS_ALL, P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE, P.OBSERVATION_MANAGE_ANY]
    archiver = await make_user(db, perms=perms)
    await with_grants(archiver, grant(ROOT, P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE, P.OBSERVATION_MANAGE_ANY))
    url = _observation_url(ObservationRoutes.OBSERVATION_ARCHIVE, observation.id)

    refused = await client.post(url, headers=headers(archiver))
    assert refused.status_code == 409 and refused.json()["code"] == "post_archived", refused.text
    await with_grants(archiver, grant(ROOT, P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE, P.OBSERVATION_MANAGE_ANY, P.POST_ARCHIVE))
    assert (await client.post(url, headers=headers(archiver))).status_code == 200


async def test_archived_observations_are_listed_only_where_observation_archive_is_held(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    live_a = await _observation(db, owner, a, ids["signal_type_id"])
    gone_a = await _observation(db, owner, a, ids["signal_type_id"], archived=True)
    live_b = await _observation(db, owner, b, ids["signal_type_id"])
    gone_b = await _observation(db, owner, b, ids["signal_type_id"], archived=True)
    perms = [P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE]
    reader = await make_user(db, perms=perms, post_id=a.id, extra_access={b.id: True})
    await with_grants(reader, grant(Node.post(a.id), *perms), grant(Node.post(b.id), P.OBSERVATION_VIEW))

    for path in (OBSERVATIONS, SEARCH_OBSERVATIONS):
        listed = _ids(await client.get(path, params={"include_deleted": "true"}, headers=headers(reader)))
        assert listed == {live_a.id, gone_a.id, live_b.id}, path

    def bands(observation_id):
        return client.get(V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES, observation_id=observation_id),
                          params={"include_deleted": "true"}, headers=headers(reader))
    assert (await bands(gone_a.id)).status_code == 200
    hidden = await bands(gone_b.id)
    assert hidden.status_code == 404 and hidden.json()["code"] == "observation_not_found", hidden.text


async def test_archived_media_is_listed_only_where_media_archive_is_held(client, db, headers, with_grants):
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    obs_a = await _observation(db, owner, a, ids["signal_type_id"])
    obs_b = await _observation(db, owner, b, ids["signal_type_id"])
    live_a, gone_a = await make_media(db, obs_a, owner), await make_media(db, obs_a, owner, is_deleted=True)
    live_b = await make_media(db, obs_b, owner)
    await make_media(db, obs_b, owner, is_deleted=True)
    perms = [P.MEDIA_VIEW, P.MEDIA_ARCHIVE]
    reader = await make_user(db, perms=perms, post_id=a.id, extra_access={b.id: True})
    await with_grants(reader, grant(Node.post(a.id), *perms), grant(Node.post(b.id), P.MEDIA_VIEW))

    for path in (MEDIA, SEARCH_MEDIA):
        listed = _ids(await client.get(path, params={"include_deleted": "true"}, headers=headers(reader)))
        assert listed == {live_a.id, gone_a.id, live_b.id}, path


async def test_regenerate_needs_media_update_at_the_post(client, db, headers, with_grants, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    video_b = await make_media(db, await _observation(db, owner, b, ids["signal_type_id"]), owner,
                               media_type="video", poster_status="ready")
    video_a = await make_media(db, await _observation(db, owner, a, ids["signal_type_id"]), owner,
                               media_type="video", poster_status="ready")
    editor = await make_user(db, perms=[ADMIN_TRUE, P.MEDIA_VIEW, P.MEDIA_UPDATE], post_id=a.id, extra_access={b.id: True})
    await with_grants(editor, grant(Node.post(a.id), P.MEDIA_VIEW, P.MEDIA_UPDATE), grant(Node.post(b.id), P.MEDIA_VIEW))

    _denied(await client.post(V1.path(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE, media_id=video_b.id), headers=headers(editor)),
            "post_write_denied", ["media.update"])
    ok = await client.post(V1.path(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE, media_id=video_a.id), headers=headers(editor))
    assert ok.status_code == 200, ok.text


# ----------------------------------------------------------------- the view and the change permission at different posts
async def _band_ids(db, observation) -> list:
    rows = await db.exec(select(ObservationFrequency.id).where(ObservationFrequency.observation_id == observation.id)
                         .order_by(ObservationFrequency.id))
    return list(rows.all())


TWO_BANDS = [("rc", "100.000", "200.000"), ("video", "300.000", "400.000")]


@pytest.mark.fit_types
async def test_band_archive_needs_observation_archive_at_the_post(client, db, headers, with_grants):
    """observation.update at A does not archive a band at A; observation.archive held at B (so the route is
    reachable) archives bands at B only"""
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    editor = await make_user(db, post_id=a.id)
    await with_grants(editor, grant(Node.post(a.id), P.OBSERVATION_VIEW, P.OBSERVATION_UPDATE),
                      grant(Node.post(b.id), P.OBSERVATION_VIEW, P.OBSERVATION_ARCHIVE))
    at_a = await make_observation(db, editor, a, ids["signal_type_id"], bands=TWO_BANDS)
    at_b = await make_observation(db, editor, b, ids["signal_type_id"], bands=TWO_BANDS)
    band_a, band_b = (await _band_ids(db, at_a))[0], (await _band_ids(db, at_b))[0]

    def band_url(route, observation, band_id):
        return V1.path(route, observation_id=observation.id, frequency_id=band_id)

    _denied(await client.post(band_url(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE, at_a, band_a), headers=headers(editor)),
            "post_write_denied", ["observation.archive"])
    updated = await client.put(band_url(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID, at_a, band_a),
                               json={"frequency_max": "250.000"}, headers=headers(editor))
    assert updated.status_code == 200, updated.text
    archived = await client.post(band_url(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE, at_b, band_b), headers=headers(editor))
    assert archived.status_code == 200, archived.text
    _denied(await client.put(band_url(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID, at_b, (await _band_ids(db, at_b))[1]),
                             json={"frequency_max": "450.000"}, headers=headers(editor)),
            "post_write_denied", ["observation.update"])


@pytest.mark.fit_types
async def test_media_view_without_media_archive_at_the_post(client, db, headers, with_grants):
    """media.view at A shows the caller's own file there, archiving it needs media.archive at A (held at B only)"""
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    uploader = await make_user(db, post_id=a.id)
    await with_grants(uploader, grant(Node.post(a.id), P.OBSERVATION_VIEW, P.MEDIA_VIEW, P.MEDIA_CREATE),
                      grant(Node.post(b.id), P.MEDIA_VIEW, P.MEDIA_ARCHIVE))
    media = await make_media(db, await _observation(db, uploader, a, ids["signal_type_id"]), uploader)

    shown = await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=media.id), headers=headers(uploader))
    assert shown.status_code == 200, shown.text
    assert media.id in _ids(await client.get(MEDIA, headers=headers(uploader)))
    _denied(await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media.id), headers=headers(uploader)),
            "post_write_denied", ["media.archive"])


async def test_observation_view_without_media_view_at_the_post(client, db, headers, with_grants):
    """observation.view at A lists the observation; its media needs media.view at A (held at B only)"""
    a, b = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_admin(db)
    observation = await _observation(db, owner, a, ids["signal_type_id"])
    media = await make_media(db, observation, owner)
    reader = await make_user(db, post_id=a.id)
    await with_grants(reader, grant(Node.post(a.id), P.OBSERVATION_VIEW), grant(Node.post(b.id), P.MEDIA_VIEW))

    assert observation.id in _ids(await client.get(OBSERVATIONS, headers=headers(reader)))
    _denied(await client.post(V1.path(MediaRoutes.LIST_ALL_ATTACHED_MEDIA), json={"observation_id": observation.id},
                              headers=headers(reader)), "post_access_denied", ["media.view"])
    _denied(await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=media.id), headers=headers(reader)),
            "post_access_denied", ["media.view"])
    for path in (MEDIA, SEARCH_MEDIA):
        assert media.id not in _ids(await client.get(path, headers=headers(reader))), path


@pytest.mark.fit_types
async def test_user_reads_of_another_user_need_user_view_covering_them(client, db, headers, with_grants):
    """The session list of a user lost the admin gate: user.view must cover the target"""
    a, b = await make_post(db), await make_post(db)
    colleague, stranger = await make_user(db, post_id=a.id), await make_user(db, post_id=b.id)
    viewer = await make_user(db, perms=[P.USER_VIEW, P.POST_VIEW, P.ROLE_VIEW], post_id=a.id)
    await with_grants(viewer, grant(Node.post(a.id), P.USER_VIEW, P.POST_VIEW), grant(ROOT, P.ROLE_VIEW))

    for route in (UserRoutes.USER_SESSIONS,):
        _denied(await client.get(V1.path(route, user_id=stranger.id), headers=headers(viewer)), "user_self_only", [])
        for target in (colleague, viewer):
            shown = await client.get(V1.path(route, user_id=target.id), headers=headers(viewer))
            assert shown.status_code == 200, (route, shown.text)


async def test_user_sessions_need_user_view_covering_the_user(client, db, headers):
    """R11 on legacy data: user.view at the home post lists the sessions of the users of that post only"""
    a, b = await make_post(db), await make_post(db)
    colleague, stranger = await make_user(db, post_id=a.id), await make_user(db, post_id=b.id)
    viewer = await make_user(db, perms=[P.USER_VIEW], post_id=a.id)
    sessions = V1.path(UserRoutes.USER_SESSIONS, user_id=stranger.id)
    _denied(await client.get(sessions, headers=headers(viewer)), "user_self_only", [])
    assert (await client.get(V1.path(UserRoutes.USER_SESSIONS, user_id=colleague.id), headers=headers(viewer))).status_code == 200
    assert (await client.get(sessions, headers=headers(await make_admin(db)))).status_code == 200


@pytest.mark.fit_types
async def test_sessions_of_an_archived_user_need_user_archive_covering_them(client, db, headers, with_grants):
    """Like GET /users/{user_id}: an archived user is shown only where user.archive covers them"""
    a = await make_post(db)
    gone = await make_user(db, post_id=a.id)
    await _archive(db, gone)
    sessions = V1.path(UserRoutes.USER_SESSIONS, user_id=gone.id)
    viewer = await make_user(db, perms=[P.USER_VIEW], post_id=a.id)
    hidden = await client.get(sessions, headers=headers(viewer))
    assert hidden.status_code == 404 and hidden.json()["code"] == "user_not_found", hidden.text

    archiver = await make_user(db, perms=[P.USER_VIEW, P.USER_ARCHIVE], post_id=a.id)
    await with_grants(archiver, grant(Node.post(a.id), P.USER_VIEW, P.USER_ARCHIVE))
    shown = await client.get(sessions, headers=headers(archiver))
    assert shown.status_code == 200, shown.text


@pytest.mark.fit_types
async def test_administration_routes_need_only_their_permissions(client, db, headers, with_grants):
    """Dictionaries, formats, roles and groups: the permissions of the route at some node, no admin.true"""
    editor = await make_user(db, perms=[P.TAG_VIEW, P.TAG_CREATE, P.SCOPE_GROUP_VIEW, P.ROLE_VIEW])
    await with_grants(editor, grant(ROOT, P.TAG_VIEW, P.TAG_CREATE, P.SCOPE_GROUP_VIEW, P.ROLE_VIEW))
    created = await client.post(V1.path(TagRoutes.CREATE), json={"name": "no-gate"}, headers=headers(editor))
    assert created.status_code == 200, created.text
    assert (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=headers(editor))).status_code == 200
    assert (await client.get(V1.path(RoleRoutes.ROOT), headers=headers(editor))).status_code == 200

    # tag.create in a role without admin.true did nothing and still does nothing
    tagger = await make_user(db, perms=[P.TAG_VIEW, P.TAG_CREATE])
    _denied(await client.post(V1.path(TagRoutes.CREATE), json={"name": "dead"}, headers=headers(tagger)),
            "permission_denied", ["tag.create"])
