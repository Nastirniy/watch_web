"""Unit management below the root (D1): scope_group.create / update / archive are scoped permissions. A write on a
group needs its scope_group permission covering the group's parent (the root for a group directly under it); a move
needs scope_group.update covering the old and the new parent. A holder at a group manages the groups inside it,
never the group itself. post_admin may hold the three (ceiling), the operator may not.
"""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, ScopeGroupRoutes, UserRoutes
from app.backend.auth import context
from app.backend.auth.context import load_scope_tree
from app.backend.models.audit import AuditLog
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.scope_group import ScopeGroup
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user

COMMANDER = [
    Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_UPDATE,
    Permissions.SCOPE_GROUP_ARCHIVE, Permissions.POST_VIEW, Permissions.POST_UPDATE, Permissions.POST_ARCHIVE,
]
WRITES = [Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_UPDATE, Permissions.SCOPE_GROUP_ARCHIVE]


async def _commander(db, node: Node):
    """A post_admin holding the unit management role at node"""
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, COMMANDER), node)
    return user


def _refused(r, action: str = None) -> None:
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "permission_required_for_action", r.text
    if action is not None:
        assert r.json()["action"] == action, r.text


async def test_corps_commander_creates_renames_moves_and_archives_brigades_of_its_corps(client, db, headers):
    corps = await make_group(db, kind="corps")
    h = headers(await _commander(db, Node.group(corps.id)))

    created = await client.post(V1.path(BrigadeRoutes.ROOT), json={"corps_id": corps.id, "name": "Brigade 1"}, headers=h)
    assert created.status_code == 201, created.text
    brigade_id = created.json()["id"]
    await make_post(db, group_id=brigade_id)
    renamed = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade_id),
                                 json={"name": "Brigade 1a", "description": "d"}, headers=h)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Brigade 1a", renamed.text
    # A move within the same corps is no move: the corps covers both ends
    same = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade_id),
                              json={"corps_id": corps.id}, headers=h)
    assert same.status_code == 200, same.text
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade_id),
                                 params={"cascade": "true"}, headers=h)
    assert archived.status_code == 200 and archived.json()["is_deleted"] is True, archived.text
    restored = await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade_id), headers=h)
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False, restored.text
    detached = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade_id), headers=h)
    assert detached.status_code == 200, detached.text


async def test_corps_commander_cannot_touch_the_corps_itself(client, db, headers):
    corps = await make_group(db, kind="corps")
    h = headers(await _commander(db, Node.group(corps.id)))

    _refused(await client.patch(V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=corps.id), json={"name": "Mine"}, headers=h),
             "update_corps")
    _refused(await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=h), "archive_corps")
    _refused(await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), params={"cascade": "true"},
                               headers=h), "archive_corps")
    _refused(await client.post(V1.path(CorpsRoutes.ROOT), json={"name": "Another"}, headers=h), "create_corps")
    await db.refresh(corps)
    assert corps.name != "Mine" and not corps.is_deleted

    # Archived by someone else: grants at an archived group stop applying, so the restore is refused before the
    # parent rule (permission_denied of require())
    corps.is_deleted = True
    db.add(corps)
    await db.commit()
    r = await client.post(V1.path(CorpsRoutes.CORPS_RESTORE, corps_id=corps.id), headers=h)
    assert r.status_code == 403, r.text


async def test_corps_commander_renames_through_the_generic_scope_group_route_by_the_same_parent_rule(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    h = headers(await _commander(db, Node.group(corps.id)))

    renamed = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id), json={"name": "Brigade 7"},
                                 headers=h)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Brigade 7", renamed.text
    _refused(await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=corps.id), json={"name": "Mine"},
                                headers=h), "update_scope_group")
    await db.refresh(corps)
    assert corps.name != "Mine"


async def test_corps_commander_cannot_move_a_brigade_to_a_corps_it_does_not_cover(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    user = await _commander(db, Node.group(corps.id))
    # It sees the other corps (the 404 of an invisible one is not the point here) and may move posts there
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.POST_VIEW,
                                                Permissions.POST_UPDATE]), Node.group(other.id))
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": other.id},
                           headers=headers(user))
    _refused(r, "move_brigade")
    assert r.json()["permission"] == "scope_group.update"
    await db.refresh(brigade)
    assert brigade.parent_id == corps.id

    # And the other way: a brigade of the other corps is not its to move in
    stranger = await make_group(db, parent_id=other.id, kind="brigade")
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=stranger.id), json={"corps_id": corps.id},
                           headers=headers(user))
    _refused(r, "move_brigade")


async def test_brigade_commander_cannot_rename_its_own_brigade(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    h = headers(await _commander(db, Node.group(brigade.id)))

    _refused(await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"name": "Mine"},
                                headers=h), "update_brigade")
    _refused(await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id),
                               params={"cascade": "true"}, headers=h), "archive_brigade")
    await db.refresh(brigade)
    assert brigade.name != "Mine" and not brigade.is_deleted


async def test_plain_groups_follow_the_same_parent_rule(client, db, headers):
    east = await make_group(db, "East")
    h = headers(await _commander(db, Node.group(east.id)))

    created = await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": "Kharkiv", "parent_id": east.id}, headers=h)
    assert created.status_code == 200, created.text
    kharkiv = created.json()["id"]
    renamed = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=kharkiv), json={"name": "Kharkiv 2"},
                                 headers=h)
    assert renamed.status_code == 200, renamed.text
    archived = await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=kharkiv), headers=h)
    assert archived.status_code == 200, archived.text
    restored = await client.post(V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=kharkiv), headers=h)
    assert restored.status_code == 200, restored.text

    # The group itself, the root, and a move out of East: its parent is the root
    _refused(await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=east.id), json={"name": "Mine"},
                                headers=h), "update_scope_group")
    _refused(await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=east.id), headers=h),
             "archive_scope_group")
    _refused(await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": "Top"}, headers=h), "create_scope_group")
    _refused(await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=kharkiv), json={"parent_id": None},
                                headers=h), "move_scope_group")


async def test_root_holder_manages_every_unit_as_before(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    h = headers(await make_admin(db))
    r = await client.patch(V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=corps.id), json={"name": "Renamed"}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": other.id},
                           headers=h)
    assert r.status_code == 200 and r.json()["corps_id"] == other.id, r.text


async def _builtin(db, user_type: UserType) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == user_type.value))).one()


async def _assign_through_api(client, db, headers, user_type: UserType, corps):
    admin = await make_admin(db)
    target = await make_user(db, user_type=user_type)
    await assign(db, target, await _builtin(db, user_type), ROOT)
    role = await make_role(db, [Permissions.SCOPE_GROUP_VIEW, *WRITES])
    return await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id),
                             json={"role_id": role.id, "scope": {"kind": "group", "id": corps.id}},
                             headers=headers(admin))


async def test_post_admin_type_may_hold_scope_group_writes(client, db, headers):
    corps = await make_group(db, kind="corps")
    r = await _assign_through_api(client, db, headers, UserType.ADMINISTRATOR, corps)
    assert r.status_code == 201, r.text
    assert r.json()["scope"]["id"] == corps.id


async def test_operator_type_may_not_hold_scope_group_writes(client, db, headers):
    corps = await make_group(db, kind="corps")
    r = await _assign_through_api(client, db, headers, UserType.OPERATOR, corps)
    assert r.status_code == 409 and r.json()["code"] == "role_exceeds_user_type", r.text
    assert set(r.json()["excess_permissions"]) >= {p.value for p in WRITES}


# ----------------------------------------------------------------- archived groups in lists (fix round 1)
async def _archived_group(db, kind: str, parent_id=None):
    group = await make_group(db, parent_id=parent_id, kind=kind)
    group.is_deleted = True
    db.add(group)
    await db.commit()
    return group


LISTS = ("corps", "brigades", "corps_brigades", "scope_groups")


def _list_urls(which: str, corps_a, corps_b) -> list:
    if which == "corps":
        return [V1.path(CorpsRoutes.ROOT)]
    if which == "brigades":
        return [V1.path(BrigadeRoutes.ROOT)]
    if which == "corps_brigades":
        return [V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=corps_a.id),
                V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=corps_b.id)]
    return [V1.path(ScopeGroupRoutes.ROOT)]


@pytest.mark.parametrize("which", LISTS)
async def test_include_deleted_shows_archived_groups_only_where_scope_group_archive_is_in_effect(
        client, db, headers, which):
    """scope_group.archive at corps A, scope_group.view at the root: archived groups inside A are listed, archived
    ones elsewhere (a brigade of B, a corps, a root plain group) are not; live groups stay as before. The root holder
    lists every archived group."""
    corps_a = await make_group(db, kind="corps")
    corps_b = await make_group(db, kind="corps")
    brigade_a = await _archived_group(db, "brigade", corps_a.id)
    brigade_b = await _archived_group(db, "brigade", corps_b.id)
    corps_z = await _archived_group(db, "corps")
    plain = await _archived_group(db, "group")
    live_brigade = await make_group(db, parent_id=corps_b.id, kind="brigade")

    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), ROOT)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_ARCHIVE]), Node.group(corps_a.id))
    admin = await make_admin(db)

    def listed(responses) -> set:
        ids = set()
        for r in responses:
            assert r.status_code == 200, r.text
            ids |= {item["id"] for item in r.json()}
        return ids

    urls = _list_urls(which, corps_a, corps_b)
    params = {"include_deleted": "true"}
    mine = listed([await client.get(url, params=params, headers=headers(user)) for url in urls])
    every = listed([await client.get(url, params=params, headers=headers(admin)) for url in urls])
    archived_elsewhere = {brigade_b.id, corps_z.id, plain.id}

    assert not mine & archived_elsewhere
    assert archived_elsewhere & every == archived_elsewhere & {
        "corps": {corps_z.id}, "brigades": {brigade_b.id}, "corps_brigades": {brigade_b.id},
        "scope_groups": archived_elsewhere,
    }[which]
    if which == "corps":
        assert {corps_a.id, corps_b.id} <= mine and corps_z.id in every
    else:
        assert brigade_a.id in mine and brigade_a.id in every
        assert live_brigade.id in mine and brigade_b.id in every


# ----------------------------------------------------------------- bug hunt 2 (A-02, A-05, A-06, C-L1)
async def _holder(db, grants):
    """A post_admin holding one custom role per (permissions, node)"""
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    for perms, node in grants:
        await assign(db, user, await make_role(db, perms), node)
    return user


def _missing_view(r, action: str) -> None:
    _refused(r, action)
    assert r.json()["missing"] == [Permissions.SCOPE_GROUP_VIEW.value], r.text


async def test_creating_a_corps_needs_scope_group_view_at_the_root_before_the_name_check(client, db, headers):
    """X.view AND X.create at the root: a holder of scope_group.create there who cannot see the root neither creates
    a corps it would never see nor learns which root names exist (403 before 409)"""
    seen = await make_group(db)
    taken = await make_group(db, "Taken corps", kind="corps")
    blind = await _holder(db, [([Permissions.SCOPE_GROUP_CREATE], ROOT),
                               ([Permissions.SCOPE_GROUP_VIEW], Node.group(seen.id))])
    for name in ("Blind corps", taken.name):
        _missing_view(await client.post(V1.path(CorpsRoutes.ROOT), json={"name": name}, headers=headers(blind)),
                      "create_corps")
    assert (await db.exec(select(ScopeGroup).where(ScopeGroup.name == "Blind corps"))).first() is None

    sighted = await _holder(db, [([Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_VIEW], ROOT)])
    r = await client.post(V1.path(CorpsRoutes.ROOT), json={"name": "Blind corps"}, headers=headers(sighted))
    assert r.status_code == 201, r.text
    r = await client.post(V1.path(CorpsRoutes.ROOT), json={"name": taken.name}, headers=headers(sighted))
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken", r.text


async def test_creating_a_root_group_needs_scope_group_view_at_the_root_before_the_name_check(client, db, headers):
    seen = await make_group(db)
    taken = await make_group(db, "Taken group")
    blind = await _holder(db, [([Permissions.SCOPE_GROUP_CREATE], ROOT),
                               ([Permissions.SCOPE_GROUP_VIEW], Node.group(seen.id))])
    for name in ("Blind group", taken.name):
        _missing_view(await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": name}, headers=headers(blind)),
                      "create_scope_group")

    sighted = await _holder(db, [([Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_VIEW], ROOT)])
    r = await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": "Blind group"}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["parent_id"] is None, r.text


async def test_moving_a_group_to_the_root_needs_scope_group_view_at_the_root(client, db, headers):
    east = await make_group(db, "East")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    post_rights = ([Permissions.POST_UPDATE, Permissions.POST_VIEW], ROOT)
    blind = await _holder(db, [([Permissions.SCOPE_GROUP_UPDATE], ROOT),
                               ([Permissions.SCOPE_GROUP_VIEW], Node.group(east.id)), post_rights])
    url = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=kharkiv.id)
    _missing_view(await client.patch(url, json={"parent_id": None}, headers=headers(blind)), "move_scope_group")
    await db.refresh(kharkiv)
    assert kharkiv.parent_id == east.id

    sighted = await _holder(db, [([Permissions.SCOPE_GROUP_UPDATE, Permissions.SCOPE_GROUP_VIEW], ROOT), post_rights])
    r = await client.patch(url, json={"parent_id": None}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["parent_id"] is None, r.text


async def test_brigade_holder_sending_its_current_corps_is_answered_like_without_it(client, db, headers):
    """corps_id equal to the brigade's own corps is no move: the same 403 update_brigade as the body without it, not
    the 404 of a corps the holder cannot see"""
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    h = headers(await _commander(db, Node.group(brigade.id)))
    url = V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id)
    _refused(await client.patch(url, json={"name": "x"}, headers=h), "update_brigade")
    _refused(await client.patch(url, json={"name": "x", "corps_id": corps.id}, headers=h), "update_brigade")
    # A real move to a corps it cannot see still answers 404
    hidden = await make_group(db, kind="corps")
    r = await client.patch(url, json={"corps_id": hidden.id}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", r.text


async def test_corps_holder_sending_the_current_corps_renames_its_brigade(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    h = headers(await _commander(db, Node.group(corps.id)))
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                           json={"name": "Renamed", "corps_id": corps.id}, headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["corps_id"] == corps.id, r.text


UNIT_ARCHIVER = [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]


async def test_cascade_archive_of_an_empty_unit_needs_no_post_rights(client, db, headers):
    """Like a detach: the request's tree shows no active post under the unit, so nothing but the unit (and its empty
    brigades) is archived and no post right is asked"""
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    archived_post = await make_post(db, group_id=brigade.id)
    archived_post.is_deleted = True
    db.add(archived_post)
    await db.commit()
    h = headers(await _holder(db, [(UNIT_ARCHIVER, Node.group(corps.id))]))
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), params={"cascade": "true"},
                          headers=h)
    assert r.status_code == 200 and r.json()["is_deleted"] is True, r.text

    empty_corps = await make_group(db, kind="corps")
    await make_group(db, parent_id=empty_corps.id, kind="brigade")
    root_holder = headers(await _holder(db, [(UNIT_ARCHIVER, ROOT)]))
    r = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=empty_corps.id), params={"cascade": "true"},
                          headers=root_holder)
    assert r.status_code == 200 and r.json()["is_deleted"] is True, r.text


async def test_cascade_archive_of_a_unit_with_a_post_still_needs_post_rights(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    h = headers(await _holder(db, [(UNIT_ARCHIVER, Node.group(corps.id))]))
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), params={"cascade": "true"},
                          headers=h)
    _refused(r, "archive_brigade")
    assert r.json()["missing"] == [Permissions.POST_ARCHIVE.value], r.text
    await db.refresh(post)
    await db.refresh(brigade)
    assert post.is_deleted is False and brigade.is_deleted is False


async def _stale_tree(monkeypatch, db) -> None:
    """Requests read the tree as it is now, before the test adds rows (a post committed after the tree was read)"""
    stale = await load_scope_tree(db)

    async def old_tree(session):
        return stale
    monkeypatch.setattr(context, "load_scope_tree", old_tree)


async def test_cascade_of_a_unit_the_tree_shows_empty_checks_a_post_that_arrived_late(client, db, headers, monkeypatch):
    """The request's tree shows an empty brigade; an active post is committed into it before the lock: the cascade
    archives it, so post.archive / post.view are asked after all (403) and nothing changes"""
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    user = await _holder(db, [(UNIT_ARCHIVER, Node.group(corps.id))])
    brigade_id = brigade.id
    await _stale_tree(monkeypatch, db)
    post = await make_post(db, group_id=brigade_id)
    post_id = post.id

    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade_id), params={"cascade": "true"},
                          headers=headers(user))
    _refused(r, "archive_brigade")
    unit_row = (await db.exec(select(ScopeGroup).where(ScopeGroup.id == brigade_id).execution_options(
        populate_existing=True))).one()
    late = (await db.exec(select(Post).where(Post.id == post_id).execution_options(populate_existing=True))).one()
    assert unit_row.is_deleted is False and late.is_deleted is False
    audited = (await db.exec(select(AuditLog).where(AuditLog.resource_type.in_(["scope_group", "post"])))).all()
    assert not [row for row in audited if row.action == "archive"]


async def test_cascade_with_a_late_post_succeeds_for_a_caller_with_the_post_rights(client, db, headers, monkeypatch):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    user = await _holder(db, [(UNIT_ARCHIVER, Node.group(corps.id)),
                              ([Permissions.POST_ARCHIVE, Permissions.POST_VIEW], Node.group(corps.id))])
    brigade_id = brigade.id
    await _stale_tree(monkeypatch, db)
    post = await make_post(db, group_id=brigade_id)
    post_id = post.id
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade_id), params={"cascade": "true"},
                          headers=headers(user))
    assert r.status_code == 200, r.text
    late = (await db.exec(select(Post).where(Post.id == post_id).execution_options(populate_existing=True))).one()
    assert late.is_deleted is True


async def test_corps_archive_without_cascade_answers_corps_has_brigades_without_post_rights(client, db, headers):
    """C-L1: the posts of its brigades never move on a corps detach (409 first); only the posts directly in the corps
    need post.update / post.view"""
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    await make_post(db, group_id=brigade.id)
    h = headers(await _holder(db, [(UNIT_ARCHIVER, ROOT)]))
    r = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=h)
    assert r.status_code == 409 and r.json()["code"] == "corps_has_brigades", r.text

    # A post directly in the corps would move under the root: post rights are asked
    await make_post(db, group_id=corps.id)
    _refused(await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=h), "archive_corps")
    await db.refresh(corps)
    assert corps.is_deleted is False


# ----------------------------------------------------------------- bug hunt 2 (move: view at the old parent)
async def test_moving_a_group_needs_scope_group_view_at_the_old_parent(client, db, headers):
    """X.view AND X.update at both ends of a move: update and post rights at both parents, view only at the new one
    and at the group itself -> 403 move_scope_group (missing scope_group.view); with view at the old parent too, 200"""
    east = await make_group(db, "East")
    west = await make_group(db, "West")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    posts = ([Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_UPDATE, Permissions.POST_VIEW], ROOT)
    url = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=kharkiv.id)
    sees = [([Permissions.SCOPE_GROUP_VIEW], Node.group(kharkiv.id)),
            ([Permissions.SCOPE_GROUP_VIEW], Node.group(west.id))]

    blind = await _holder(db, [posts, *sees])
    _missing_view(await client.patch(url, json={"parent_id": west.id}, headers=headers(blind)), "move_scope_group")
    await db.refresh(kharkiv)
    assert kharkiv.parent_id == east.id

    sighted = await _holder(db, [posts, *sees, ([Permissions.SCOPE_GROUP_VIEW], Node.group(east.id))])
    r = await client.patch(url, json={"parent_id": west.id}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["parent_id"] == west.id, r.text


async def test_moving_a_root_group_needs_scope_group_view_at_the_root_as_the_old_parent(client, db, headers):
    west = await make_group(db, "West")
    lone = await make_group(db, "Lone")
    posts = ([Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_UPDATE, Permissions.POST_VIEW], ROOT)
    url = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=lone.id)
    sees = [([Permissions.SCOPE_GROUP_VIEW], Node.group(lone.id)),
            ([Permissions.SCOPE_GROUP_VIEW], Node.group(west.id))]

    blind = await _holder(db, [posts, *sees])
    _missing_view(await client.patch(url, json={"parent_id": west.id}, headers=headers(blind)), "move_scope_group")

    sighted = await _holder(db, [posts, *sees, ([Permissions.SCOPE_GROUP_VIEW], ROOT)])
    r = await client.patch(url, json={"parent_id": west.id}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["parent_id"] == west.id, r.text


async def test_moving_a_brigade_needs_scope_group_view_at_the_old_corps(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    posts = ([Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_UPDATE, Permissions.POST_VIEW], ROOT)
    url = V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id)
    sees = [([Permissions.SCOPE_GROUP_VIEW], Node.group(brigade.id)),
            ([Permissions.SCOPE_GROUP_VIEW], Node.group(other.id))]

    blind = await _holder(db, [posts, *sees])
    _missing_view(await client.patch(url, json={"corps_id": other.id}, headers=headers(blind)), "move_brigade")
    await db.refresh(brigade)
    assert brigade.parent_id == corps.id

    sighted = await _holder(db, [posts, *sees, ([Permissions.SCOPE_GROUP_VIEW], Node.group(corps.id))])
    r = await client.patch(url, json={"corps_id": other.id}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["corps_id"] == other.id, r.text


# ----------------------------------------------------------------- bug hunt 2 (rename: view at the parent)
async def test_renaming_a_corps_needs_scope_group_view_at_the_root_before_the_name_check(client, db, headers):
    """A rename is checked against the names beside the unit (409 is an oracle on them): update at the parent and
    view only at the unit itself -> 403 update_corps (missing scope_group.view), also for a taken name; a change that
    leaves the name alone needs no view at the parent"""
    corps = await make_group(db, "Corps A", kind="corps")
    taken = await make_group(db, "Corps B", kind="corps")
    url = V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=corps.id)
    grants = [([Permissions.SCOPE_GROUP_UPDATE], ROOT), ([Permissions.SCOPE_GROUP_VIEW], Node.group(corps.id))]

    blind = await _holder(db, grants)
    for name in ("Corps C", taken.name):
        _missing_view(await client.patch(url, json={"name": name}, headers=headers(blind)), "update_corps")
    await db.refresh(corps)
    assert corps.name == "Corps A"
    r = await client.patch(url, json={"description": "only the text"}, headers=headers(blind))
    assert r.status_code == 200, r.text

    sighted = await _holder(db, [*grants, ([Permissions.SCOPE_GROUP_VIEW], ROOT)])
    r = await client.patch(url, json={"name": taken.name}, headers=headers(sighted))
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken", r.text
    r = await client.patch(url, json={"name": "Corps C"}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["name"] == "Corps C", r.text


@pytest.mark.parametrize("under_root", [True, False])
async def test_renaming_a_group_in_place_needs_scope_group_view_at_its_parent(client, db, headers, under_root):
    east = await make_group(db, "East")
    parent_id = None if under_root else east.id
    group = await make_group(db, "Kharkiv", parent_id=parent_id)
    taken = await make_group(db, "Lviv", parent_id=parent_id)
    url = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=group.id)
    parent = ROOT if under_root else Node.group(east.id)
    grants = [([Permissions.SCOPE_GROUP_UPDATE], ROOT), ([Permissions.SCOPE_GROUP_VIEW], Node.group(group.id))]

    blind = await _holder(db, grants)
    for name in ("Poltava", taken.name):
        _missing_view(await client.patch(url, json={"name": name}, headers=headers(blind)), "update_scope_group")
    await db.refresh(group)
    assert group.name == "Kharkiv"
    r = await client.patch(url, json={"description": "only the text"}, headers=headers(blind))
    assert r.status_code == 200, r.text

    sighted = await _holder(db, [*grants, ([Permissions.SCOPE_GROUP_VIEW], parent)])
    r = await client.patch(url, json={"name": taken.name}, headers=headers(sighted))
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken", r.text
    r = await client.patch(url, json={"name": "Poltava"}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["name"] == "Poltava", r.text


async def test_renaming_a_brigade_needs_scope_group_view_at_its_corps_before_the_name_check(client, db, headers):
    corps = await make_group(db, "Corps A", kind="corps")
    brigade = await make_group(db, "Brigade 1", parent_id=corps.id, kind="brigade")
    taken = await make_group(db, "Brigade 2", parent_id=corps.id, kind="brigade")
    url = V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id)
    grants = [([Permissions.SCOPE_GROUP_UPDATE], Node.group(corps.id)),
              ([Permissions.SCOPE_GROUP_VIEW], Node.group(brigade.id))]

    blind = await _holder(db, grants)
    for name in ("Brigade 3", taken.name):
        _missing_view(await client.patch(url, json={"name": name}, headers=headers(blind)), "update_brigade")
    await db.refresh(brigade)
    assert brigade.name == "Brigade 1"
    r = await client.patch(url, json={"description": "only the text"}, headers=headers(blind))
    assert r.status_code == 200, r.text

    sighted = await _holder(db, [*grants, ([Permissions.SCOPE_GROUP_VIEW], Node.group(corps.id))])
    r = await client.patch(url, json={"name": taken.name}, headers=headers(sighted))
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken", r.text
    r = await client.patch(url, json={"name": "Brigade 3"}, headers=headers(sighted))
    assert r.status_code == 200 and r.json()["name"] == "Brigade 3", r.text
