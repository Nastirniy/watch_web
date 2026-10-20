"""Unit management below the root (D1): scope_group.create / update / archive are scoped permissions. A write on a
group needs its scope_group permission covering the group's parent (the root for a group directly under it); a move
needs scope_group.update covering the old and the new parent. A holder at a group manages the groups inside it,
never the group itself. post_admin may hold the three (ceiling), the operator may not.
"""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, ScopeGroupRoutes, UserRoutes
from app.backend.models.role import Role
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user

COMMANDER = [
    Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_UPDATE,
    Permissions.SCOPE_GROUP_ARCHIVE, Permissions.POST_VIEW, Permissions.POST_UPDATE, Permissions.POST_ARCHIVE,
]
WRITES = [Permissions.SCOPE_GROUP_CREATE, Permissions.SCOPE_GROUP_UPDATE, Permissions.SCOPE_GROUP_ARCHIVE]


async def _commander(db, node: Node):
    """A post_admin holding the unit management role at node"""
    user = await make_user(db, user_type=UserType.POST_ADMIN)
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
    r = await _assign_through_api(client, db, headers, UserType.POST_ADMIN, corps)
    assert r.status_code == 201, r.text
    assert r.json()["scope"]["id"] == corps.id


async def test_operator_type_may_not_hold_scope_group_writes(client, db, headers):
    corps = await make_group(db, kind="corps")
    r = await _assign_through_api(client, db, headers, UserType.OPERATOR, corps)
    assert r.status_code == 409 and r.json()["code"] == "role_exceeds_user_type", r.text
    assert set(r.json()["excess_permissions"]) >= {p.value for p in WRITES}
