"""The built-in admin role, grants within the caller's own permissions, the last administrator, audit of user changes.

- The admin role's name and permission set are immutable through the API (the name is the stable key of the archive,
  delete and seeding guards; seeding grants it every permission).
- A caller grants or revokes only permissions they hold: roles assigned to a user, permissions added to or removed
  from a role, a role archived or restored (its permissions are granted to or revoked from every holder).
- A caller writes to another user (any field, post access, archive state, sessions) only when that user holds no
  permission the caller lacks.
- No change may leave no active administrator, and nobody removes admin.true from themselves.
"""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.privileges import check_an_admin_remains
from app.backend.auth.service import AuthService
from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import RoleRoutes, UserRoutes
from app.backend.errors.problems import ApiError
from app.backend.models.audit import AuditLog
from app.backend.models.role import Permission, Role, RolePermission, UserRole
from app.backend.models.user import User, UserSession
from app.backend.models.user_post_access import UserPostAccess
from tests.helpers import make_admin, make_post, make_role, make_user
from tests.test_username_validation import make_certificate

ROLES = V1.path(RoleRoutes.ROOT)
ROLE = V1.path(RoleRoutes.ROLE_BY_ID)
ROLE_ARCHIVE = V1.path(RoleRoutes.ROLE_ARCHIVE)
ROLE_RESTORE = V1.path(RoleRoutes.ROLE_RESTORE)
ROLE_PERMISSION = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID)
USERS = V1.path(UserRoutes.ROOT)
USER = V1.path(UserRoutes.USER_BY_ID)
USER_ARCHIVE = V1.path(UserRoutes.USER_ARCHIVE)
USER_RESTORE = V1.path(UserRoutes.USER_RESTORE)
USER_POST_ACCESS = V1.path(UserRoutes.USER_POST_ACCESS_BY_POST)
USER_SESSIONS_END = V1.path(UserRoutes.USER_SESSIONS_END)
USER_SESSION_END = V1.path(UserRoutes.USER_SESSION_END)


async def admin_role(db) -> Role:
    return (await db.exec(select(Role).where(Role.name == "admin"))).one()


async def permission_id(db, name: str) -> int:
    return (await db.exec(select(Permission.id).where(Permission.name == name))).one()


async def stored_roles(db, user_id: int) -> list:
    return sorted((await db.exec(select(UserRole.role_id).where(UserRole.user_id == user_id))).all())


async def stored_permissions(db, role_id: int) -> list:
    rows = await db.exec(
        select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role_id)
    )
    return sorted(rows.all())


def certificate_file(common_name: str) -> dict:
    return {"certificate": ("cert.pem", make_certificate(common_name), "application/x-pem-file")}


def problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code, body
    return body


# ----------------------------------------------------------------- the admin role's name

@pytest.mark.asyncio
async def test_admin_role_cannot_be_renamed(client, db, headers):
    """Reproduced: after a rename the archive guard no longer recognised the role and archiving it answered 200"""
    admin = await make_admin(db)
    role = await admin_role(db)

    problem(await client.patch(ROLE.format(role_id=role.id), headers=headers(admin), json={"name": "chief"}),
            409, "builtin_role_not_renamable")
    assert (await db.exec(select(Role.name).where(Role.id == role.id))).one() == "admin"
    problem(await client.post(ROLE_ARCHIVE.format(role_id=role.id), headers=headers(admin)), 409, "builtin_role_not_archivable")

    # the unchanged name and the other fields stay editable
    same = await client.patch(ROLE.format(role_id=role.id), headers=headers(admin),
                              json={"name": "admin", "display_name": "Administrators"})
    assert same.status_code == 200, same.text
    assert same.json()["display_name"] == "Administrators"


@pytest.mark.asyncio
async def test_admin_role_permissions_cannot_be_changed(client, db, headers):
    """Review: a limited admin removed admin.true from the admin role and every full administrator lost the area"""
    admin = await make_admin(db)
    editor = await make_user(db, perms=["admin.true", "role.view", "role.update"])
    role = await admin_role(db)
    before = await stored_permissions(db, role.id)
    is_admin = await permission_id(db, "admin.true")
    every = sorted((await db.exec(select(Permission.id))).all())

    for caller in (editor, admin):
        problem(await client.delete(ROLE_PERMISSION.format(role_id=role.id, permission_id=is_admin), headers=headers(caller)),
                409, "builtin_role_permissions_fixed")
        problem(await client.patch(ROLE.format(role_id=role.id), headers=headers(caller),
                                   json={"permission_ids": [pid for pid in every if pid != is_admin]}),
                409, "builtin_role_permissions_fixed")
    assert await stored_permissions(db, role.id) == before

    # the same set (in any order) and the other fields stay editable
    same = await client.patch(ROLE.format(role_id=role.id), headers=headers(admin),
                              json={"permission_ids": list(reversed(every)), "display_name": "Administrators"})
    assert same.status_code == 200, same.text
    assert await stored_permissions(db, role.id) == before


@pytest.mark.asyncio
async def test_admin_role_gains_permissions_only_by_seeding(client, db, headers):
    admin = await make_admin(db)
    role = await admin_role(db)
    audit_view = await permission_id(db, "audit.view")
    await db.execute(RolePermission.__table__.delete().where(
        RolePermission.role_id == role.id, RolePermission.permission_id == audit_view))
    await db.commit()

    problem(await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=audit_view), headers=headers(admin)),
            409, "builtin_role_permissions_fixed")
    assert "audit.view" not in await stored_permissions(db, role.id)

    await seed_roles_and_permissions(db)
    assert "audit.view" in await stored_permissions(db, role.id)
    # granting what it already holds stays an idempotent 200
    held = await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=audit_view), headers=headers(admin))
    assert held.status_code == 200, held.text


# ----------------------------------------------------------------- grants within the caller's own permissions

@pytest.mark.asyncio
async def test_user_update_holder_cannot_give_themselves_the_admin_role(client, db, headers):
    """Reproduced: user.update with admin.true assigned the admin role to the caller"""
    await make_admin(db)
    manager = await make_user(db, perms=["admin.true", "user.view", "user.update"])
    own_role = (await stored_roles(db, manager.id))[0]
    full = await admin_role(db)

    response = await client.patch(USER.format(user_id=manager.id), headers=headers(manager),
                                  data={"role_ids": [own_role, full.id]})
    body = problem(response, 403, "grant_exceeds_own_permissions")
    assert "audit.view" in body["missing"] and "user.update" not in body["missing"]
    assert await stored_roles(db, manager.id) == [own_role]


@pytest.mark.asyncio
async def test_roles_within_the_callers_permissions_can_be_assigned(client, db, headers):
    manager = await make_user(db, perms=["admin.true", "user.view", "user.update", "tag.view"])
    viewer = await make_role(db, ["tag.view"])
    target = await make_user(db, perms=["user.view"])

    response = await client.patch(USER.format(user_id=target.id), headers=headers(manager), data={"role_ids": [viewer.id]})
    assert response.status_code == 200, response.text
    assert await stored_roles(db, target.id) == [viewer.id]


@pytest.mark.asyncio
async def test_create_user_only_with_roles_within_the_callers_permissions(client, db, headers):
    creator = await make_user(db, perms=["admin.true", "user.view", "user.create"])
    full = await admin_role(db)
    response = await client.post(USERS, headers=headers(creator), files=certificate_file("cn-would-be-admin"),
                                 data={"username": "would_be_admin", "username_alias": "Would be", "role_ids": [full.id]})
    body = problem(response, 403, "grant_exceeds_own_permissions")
    assert "audit.view" in body["missing"]
    assert (await db.exec(select(User).where(User.username == "would_be_admin"))).first() is None

    viewer = await make_role(db, ["user.view"])
    allowed = await client.post(USERS, headers=headers(creator), files=certificate_file("cn-plain-viewer"),
                                data={"username": "plain_viewer", "username_alias": "Viewer", "role_ids": [viewer.id]})
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_role_update_holder_cannot_extend_their_own_role(client, db, headers):
    """Reproduced: role.update with admin.true added every permission to the caller's own role"""
    await make_admin(db)
    editor = await make_user(db, perms=["admin.true", "role.view", "role.update"])
    own_role = (await stored_roles(db, editor.id))[0]
    before = await stored_permissions(db, own_role)
    every = list((await db.exec(select(Permission.id))).all())

    body = problem(await client.patch(ROLE.format(role_id=own_role), headers=headers(editor), json={"permission_ids": every}),
                   403, "grant_exceeds_own_permissions")
    assert "user.create" in body["missing"] and "role.update" not in body["missing"]
    body = problem(await client.put(ROLE_PERMISSION.format(role_id=own_role, permission_id=await permission_id(db, "audit.view")),
                                    headers=headers(editor)),
                   403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["audit.view"]
    assert await stored_permissions(db, own_role) == before


@pytest.mark.asyncio
async def test_role_changes_need_every_permission_they_add_or_remove(client, db, headers):
    editor = await make_user(db, perms=["admin.true", "role.view", "role.update", "tag.view", "tag.create"])
    role = await make_role(db, ["tag.view", "audit.view"])
    tag_view, tag_create, audit_view = [await permission_id(db, n) for n in ("tag.view", "tag.create", "audit.view")]

    # a permission the caller lacks may stay on the role: only what the change adds or removes counts
    added = await client.put(ROLE_PERMISSION.format(role_id=role.id, permission_id=tag_create), headers=headers(editor))
    assert added.status_code == 200, added.text
    patched = await client.patch(ROLE.format(role_id=role.id), headers=headers(editor),
                                 json={"permission_ids": [tag_view, audit_view]})
    assert patched.status_code == 200, patched.text

    # removing one the caller lacks is a decision about it too
    body = problem(await client.delete(ROLE_PERMISSION.format(role_id=role.id, permission_id=audit_view), headers=headers(editor)),
                   403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["audit.view"]
    body = problem(await client.patch(ROLE.format(role_id=role.id), headers=headers(editor), json={"permission_ids": [tag_view]}),
                   403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["audit.view"]
    assert await stored_permissions(db, role.id) == ["audit.view", "tag.view"]


@pytest.mark.asyncio
async def test_create_role_only_with_held_permissions(client, db, headers):
    creator = await make_user(db, perms=["admin.true", "role.view", "role.create"])
    body = problem(await client.post(ROLES, headers=headers(creator), json={
        "name": "auditors", "display_name": "Auditors", "permission_ids": [await permission_id(db, "audit.view")]}),
        403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["audit.view"]
    assert (await db.exec(select(Role).where(Role.name == "auditors"))).first() is None

    allowed = await client.post(ROLES, headers=headers(creator), json={
        "name": "role_viewers", "display_name": "Role viewers", "permission_ids": [await permission_id(db, "role.view")]})
    assert allowed.status_code == 201, allowed.text


@pytest.mark.asyncio
async def test_archive_and_restore_of_a_role_need_every_permission_of_the_role(client, db, headers):
    admin = await make_admin(db)
    archiver = await make_user(db, perms=["admin.true", "role.view", "role.archive", "tag.view"])
    beyond = await make_role(db, ["tag.view", "audit.view"])
    within = await make_role(db, ["tag.view"])

    body = problem(await client.post(ROLE_ARCHIVE.format(role_id=beyond.id), headers=headers(archiver)),
                   403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["audit.view"]
    assert (await client.post(ROLE_ARCHIVE.format(role_id=within.id), headers=headers(archiver))).status_code == 200
    assert (await client.post(ROLE_RESTORE.format(role_id=within.id), headers=headers(archiver))).status_code == 200

    assert (await client.post(ROLE_ARCHIVE.format(role_id=beyond.id), headers=headers(admin))).status_code == 200
    body = problem(await client.post(ROLE_RESTORE.format(role_id=beyond.id), headers=headers(archiver)),
                   403, "grant_exceeds_own_permissions")
    assert body["missing"] == ["audit.view"]
    assert (await db.exec(select(Role.is_deleted).where(Role.id == beyond.id))).one() is True


# ----------------------------------------------------------------- users holding more than the caller

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role_ids", "certificate", "is_active", "username", "username_alias", "post_id"])
async def test_limited_admin_cannot_change_a_more_privileged_user(client, db, headers, change):
    post = await make_post(db)
    admin = await make_admin(db)
    admin_id = admin.id
    stored_before = (admin.username, admin.username_alias, admin.post_id, admin.is_active, admin.pki_certificate_fingerprint_hash)
    manager = await make_user(db, perms=["admin.true", "user.view", "user.update"])
    manager_role = (await stored_roles(db, manager.id))[0]
    roles_before = await stored_roles(db, admin_id)
    request = {
        "role_ids": {"data": {"role_ids": [manager_role]}},
        "certificate": {"files": certificate_file("cn-taken-over")},
        "is_active": {"data": {"is_active": "false"}},
        "username": {"data": {"username": "renamed_chief"}},
        "username_alias": {"data": {"username_alias": "Chief"}},
        "post_id": {"data": {"post_id": str(post.id)}},
    }[change]

    body = problem(await client.patch(USER.format(user_id=admin_id), headers=headers(manager), **request),
                   403, "target_user_more_privileged")
    assert "audit.view" in body["missing"]
    row = (await db.exec(select(User.username, User.username_alias, User.post_id, User.is_active,
                                User.pki_certificate_fingerprint_hash).where(User.id == admin_id))).one()
    assert tuple(row) == stored_before
    assert await stored_roles(db, admin_id) == roles_before


@pytest.mark.asyncio
async def test_a_limited_admin_edits_their_own_account(client, db, headers):
    """The caller acting on themselves is not a more privileged target"""
    await make_admin(db)
    manager = await make_user(db, perms=["admin.true", "user.view", "user.update"])
    response = await client.patch(USER.format(user_id=manager.id), headers=headers(manager), data={"username_alias": "Me"})
    assert response.status_code == 200, response.text
    assert response.json()["username_alias"] == "Me"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["put_post_access", "delete_post_access", "end_sessions", "end_session"])
async def test_limited_admin_cannot_touch_post_access_or_sessions_of_a_more_privileged_user(client, db, headers, route):
    post = await make_post(db)
    admin = await make_admin(db)
    peer = await make_user(db, perms=["user.view"])
    manager = await make_user(db, perms=["admin.true", "user.view", "user.update", "post.view"])
    for target in (admin, peer):
        db.add(UserPostAccess(user_id=target.id, post_id=post.id, can_write=False))
    await db.commit()
    sessions = {target.id: await AuthService.create_session(db, target) for target in (admin, peer)}

    async def call(target):
        if route == "put_post_access":
            return await client.put(USER_POST_ACCESS.format(user_id=target.id, post_id=post.id),
                                    headers=headers(manager), json={"can_write": True})
        if route == "delete_post_access":
            return await client.delete(USER_POST_ACCESS.format(user_id=target.id, post_id=post.id), headers=headers(manager))
        if route == "end_sessions":
            return await client.post(USER_SESSIONS_END.format(user_id=target.id), headers=headers(manager))
        return await client.post(USER_SESSION_END.format(user_id=target.id, session_id=sessions[target.id].id),
                                 headers=headers(manager))

    async def state(target):
        access = (await db.exec(select(UserPostAccess.can_write).where(
            UserPostAccess.user_id == target.id, UserPostAccess.post_id == post.id))).all()
        ended = (await db.exec(select(UserSession.ended_at).where(UserSession.id == sessions[target.id].id))).one()
        return access, ended

    before = await state(admin)
    body = problem(await call(admin), 403, "target_user_more_privileged")
    assert "audit.view" in body["missing"]
    assert await state(admin) == before

    peer_before = await state(peer)
    allowed = await call(peer)
    assert allowed.status_code in (200, 204), allowed.text
    assert await state(peer) != peer_before


@pytest.mark.asyncio
async def test_limited_admin_cannot_archive_or_restore_a_more_privileged_user(client, db, headers):
    admin = await make_admin(db)
    second_admin = await make_admin(db)
    archiver = await make_user(db, perms=["admin.true", "user.view", "user.archive"])
    peer = await make_user(db, perms=["user.view"])

    body = problem(await client.post(USER_ARCHIVE.format(user_id=admin.id), headers=headers(archiver)),
                   403, "target_user_more_privileged")
    assert "audit.view" in body["missing"]
    assert (await client.post(USER_ARCHIVE.format(user_id=peer.id), headers=headers(archiver))).status_code == 200
    assert (await client.post(USER_RESTORE.format(user_id=peer.id), headers=headers(archiver))).status_code == 200

    assert (await client.post(USER_ARCHIVE.format(user_id=second_admin.id), headers=headers(admin))).status_code == 200
    problem(await client.post(USER_RESTORE.format(user_id=second_admin.id), headers=headers(archiver)),
            403, "target_user_more_privileged")
    assert (await db.exec(select(User.is_deleted).where(User.id == second_admin.id))).one() is True


# ----------------------------------------------------------------- the last administrator

@pytest.mark.asyncio
async def test_the_only_admin_cannot_replace_their_admin_role(client, db, headers):
    """Reproduced: the only administrator replaced their roles and nobody held admin.true any more"""
    admin = await make_admin(db)
    full = await admin_role(db)
    other = await make_role(db, ["user.view"])

    problem(await client.patch(USER.format(user_id=admin.id), headers=headers(admin), data={"role_ids": [other.id]}),
            409, "last_admin_required")
    assert await stored_roles(db, admin.id) == [full.id]


@pytest.mark.asyncio
async def test_an_admin_cannot_drop_their_own_admin_while_others_remain(client, db, headers):
    admin = await make_admin(db)
    peer = await make_admin(db)
    full = await admin_role(db)
    other = await make_role(db, ["user.view"])

    problem(await client.patch(USER.format(user_id=admin.id), headers=headers(admin), data={"role_ids": [other.id]}),
            409, "cannot_remove_own_admin")
    assert await stored_roles(db, admin.id) == [full.id]

    theirs = await client.patch(USER.format(user_id=peer.id), headers=headers(admin), data={"role_ids": [other.id]})
    assert theirs.status_code == 200, theirs.text
    assert await stored_roles(db, peer.id) == [other.id]


@pytest.mark.asyncio
async def test_the_only_admin_cannot_archive_or_strip_the_role_that_makes_them_admin(client, db, headers):
    """Reproduced: the only administrator archived the role that granted them admin.true"""
    keeper_role = await make_role(db, ["admin.true", "role.view", "role.update", "role.archive"])
    keeper = await make_user(db, role=keeper_role)
    is_admin, role_view, role_update, role_archive = [
        await permission_id(db, n) for n in ("admin.true", "role.view", "role.update", "role.archive")]

    problem(await client.post(ROLE_ARCHIVE.format(role_id=keeper_role.id), headers=headers(keeper)), 409, "last_admin_required")
    problem(await client.delete(ROLE_PERMISSION.format(role_id=keeper_role.id, permission_id=is_admin), headers=headers(keeper)),
            409, "last_admin_required")
    problem(await client.patch(ROLE.format(role_id=keeper_role.id), headers=headers(keeper),
                               json={"permission_ids": [role_view, role_update, role_archive]}),
            409, "last_admin_required")
    assert (await db.exec(select(Role.is_deleted).where(Role.id == keeper_role.id))).one() is False
    assert "admin.true" in await stored_permissions(db, keeper_role.id)

    # with another administrator around it is still the caller's own admin.true
    admin = await make_admin(db)
    problem(await client.post(ROLE_ARCHIVE.format(role_id=keeper_role.id), headers=headers(keeper)), 409, "cannot_remove_own_admin")
    # someone else may take it away
    assert (await client.post(ROLE_ARCHIVE.format(role_id=keeper_role.id), headers=headers(admin))).status_code == 200


@pytest.mark.asyncio
async def test_guard_refuses_a_change_that_leaves_no_active_admin(db):
    """Deactivating or archiving the last administrator: the routes refuse doing it to oneself first, the guard still holds"""
    admin = await make_admin(db)
    await check_an_admin_remains(db)

    for field, value in (("is_active", False), ("is_deleted", True)):
        setattr(admin, field, value)
        db.add(admin)
        with pytest.raises(ApiError) as caught:
            await check_an_admin_remains(db)
        assert caught.value.code is ErrorCode.LAST_ADMIN_REQUIRED
        await db.rollback()
        await db.refresh(admin)


# ----------------------------------------------------------------- audit of user changes

@pytest.mark.asyncio
async def test_user_audit_records_roles_and_certificate_replacement(client, db, headers):
    admin = await make_admin(db)
    first = await make_role(db, ["tag.view"])
    second = await make_role(db, ["tag.create"])

    created = await client.post(USERS, headers=headers(admin), files=certificate_file("cn-audited"),
                                data={"username": "audited", "username_alias": "Audited", "role_ids": [first.id]})
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]

    async def audit_rows(action):
        rows = await db.exec(select(AuditLog).where(
            AuditLog.resource_type == "user", AuditLog.resource_id == user_id, AuditLog.action == action).order_by(AuditLog.id))
        return rows.all()

    (create_row,) = await audit_rows("create")
    assert create_row.new_values["role_ids"] == [first.id]

    changed = await client.patch(USER.format(user_id=user_id), headers=headers(admin),
                                 data={"role_ids": [second.id]}, files=certificate_file("cn-audited-new"))
    assert changed.status_code == 200, changed.text
    alias = await client.patch(USER.format(user_id=user_id), headers=headers(admin), data={"username_alias": "Renamed"})
    assert alias.status_code == 200, alias.text

    role_change, alias_change = await audit_rows("update")
    assert role_change.old_values["role_ids"] == [first.id]
    assert role_change.new_values["role_ids"] == [second.id]
    assert role_change.new_values["certificate_replaced"] is True
    assert alias_change.old_values["role_ids"] == alias_change.new_values["role_ids"] == [second.id]
    assert alias_change.new_values["certificate_replaced"] is False
