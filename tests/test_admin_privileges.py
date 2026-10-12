"""Built-in roles, grants within the caller's own permissions at the node, target users, the last administrator, the
default assignment of a new user, audit of user changes (auth/privileges.py, spec section 6).

- A built-in role's name and permission set are immutable through the API (seeding owns them).
- A caller grants or revokes only permissions they hold: at the node of an assignment (check_grant_at: a new user's
  default assignment, the assignment API), anywhere for role edits (check_grant: role.* are system permissions).
- A caller changes another user only with the route's user.* permission covering them and when they could grant every
  assignment the user has (check_target_user, 403 target_user_more_privileged).
- An active, non-archived user always holds the built-in admin role at the root, and nobody removes it from themselves.
"""
import pytest
from sqlmodel import delete, select

from app.backend.api.v1 import V1
from app.backend.auth.context import load_auth_context
from app.backend.auth.privileges import (
    check_admin_kept, check_an_admin_remains, check_grant_at, check_target_user, holds_admin_at_root,
)
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.service import AuthService
from app.backend.auth.user_types import USER_TYPES
from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import UserType
from app.backend.enums.routes import RoleRoutes, UserRoutes
from app.backend.errors.problems import ApiError
from app.backend.models.audit import AuditLog
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.user import User, UserSession
from tests.helpers import ADMIN_TRUE, assign, make_admin, make_post, make_role, make_user
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
USER_SESSIONS_END = V1.path(UserRoutes.USER_SESSIONS_END)
USER_SESSION_END = V1.path(UserRoutes.USER_SESSION_END)
VIEW = sorted(USER_TYPES[UserType.VIEWER].base_permissions)


async def builtin_role(db, key: str = "admin") -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == key))).one()


async def permission_id(db, name: str) -> int:
    return (await db.exec(select(Permission.id).where(Permission.name == name))).one()


async def stored_assignments(db, user_id: int) -> set:
    """(role id, scope key) of every assignment of the user"""
    rows = await db.exec(select(RoleAssignment.role_id, RoleAssignment.scope_key).where(RoleAssignment.user_id == user_id))
    return set(rows.all())


async def own_role_id(db, user_id: int) -> int:
    (role_id,) = {role_id for role_id, _ in await stored_assignments(db, user_id)}
    return role_id


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


def refused(call, code) -> ApiError:
    with pytest.raises(ApiError) as caught:
        call()
    assert caught.value.code is code
    return caught.value


# ----------------------------------------------------------------- built-in roles

@pytest.mark.asyncio
async def test_admin_role_cannot_be_renamed(client, db, headers):
    """Reproduced: after a rename the archive guard no longer recognised the role and archiving it answered 200"""
    admin = await make_admin(db)
    role = await builtin_role(db)

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
    """Review: a limited admin removed admin.true from the admin role and every full administrator lost the area.
    Now no permission of a built-in role can be removed, by anybody"""
    admin = await make_admin(db)
    editor = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.update"])
    role = await builtin_role(db)
    before = await stored_permissions(db, role.id)
    audit_view = await permission_id(db, "audit.view")
    every = sorted((await db.exec(select(Permission.id))).all())

    for caller in (editor, admin):
        problem(await client.delete(ROLE_PERMISSION.format(role_id=role.id, permission_id=audit_view), headers=headers(caller)),
                409, "builtin_role_permissions_fixed")
        problem(await client.patch(ROLE.format(role_id=role.id), headers=headers(caller),
                                   json={"permission_ids": [pid for pid in every if pid != audit_view]}),
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
    role = await builtin_role(db)
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
async def test_user_update_holder_cannot_grant_the_admin_role(db):
    """Reproduced: user.update with admin.true assigned the admin role to the caller. An assignment needs every
    permission of the role at its node (check_grant_at, the rule of the assignment API)"""
    manager = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.update"])
    auth = await load_auth_context(db, manager)
    full = await stored_permissions(db, (await builtin_role(db)).id)

    error = refused(lambda: check_grant_at(auth, full, ROOT), AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS)
    missing = error.params["missing"]
    assert "audit.view" in missing and "user.update" not in missing and missing == sorted(missing)


@pytest.mark.asyncio
async def test_grants_are_checked_at_the_node(db):
    a, b = await make_post(db), await make_post(db)
    manager = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.update", "tag.view"])
    auth = await load_auth_context(db, manager)
    for node in (ROOT, Node.post(a.id)):
        check_grant_at(auth, ["tag.view", "user.view"], node)   # held at the root: covers every node

    local = await make_user(db, perms=["user.view", "observation.view"], post_id=a.id)
    auth = await load_auth_context(db, local)
    check_grant_at(auth, ["user.view", "observation.view"], Node.post(a.id))
    for node in (Node.post(b.id), ROOT):
        error = refused(lambda: check_grant_at(auth, ["observation.view", "user.view"], node),
                        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS)
        assert error.params["missing"] == ["observation.view", "user.view"]


@pytest.mark.asyncio
async def test_create_user_only_with_a_base_role_within_the_callers_permissions(client, db, headers):
    post = await make_post(db)
    creator = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.create", *VIEW], post_id=post.id)

    for name, fields in (("would_be_admin", {"user_type": "admin"}), ("rootless_viewer", {"user_type": "viewer"})):
        response = await client.post(USERS, headers=headers(creator), files=certificate_file(f"cn-{name}"),
                                     data={"username": name, "username_alias": "Would be", **fields})
        body = problem(response, 403, "grant_exceeds_own_permissions")
        assert body["missing"], body
        assert (await db.exec(select(User).where(User.username == name))).first() is None
    # the viewer at the root needs the views at the root; the caller holds them at the home post only
    assert "observation.view" in body["missing"] and "post.view" not in body["missing"]

    allowed = await client.post(USERS, headers=headers(creator), files=certificate_file("cn-plain-viewer"),
                                data={"username": "plain_viewer", "username_alias": "Viewer", "user_type": "viewer",
                                      "post_id": str(post.id)})
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_create_user_needs_user_create_covering_the_node(client, db, headers):
    a, b = await make_post(db), await make_post(db)
    creator = await make_user(db)
    await assign(db, creator, await make_role(db, ["user.view", "user.create", *VIEW]), Node.post(a.id))

    outside = await client.post(USERS, headers=headers(creator), files=certificate_file("cn-outside"),
                                data={"username": "outside", "username_alias": "Outside", "user_type": "viewer",
                                      "post_id": str(b.id)})
    body = problem(outside, 403, "permission_required_for_action")
    assert body["permission"] == "user.create" and body["action"] == "create_user" and body["missing"] == ["user.create"]
    inside = await client.post(USERS, headers=headers(creator), files=certificate_file("cn-inside"),
                               data={"username": "inside", "username_alias": "Inside", "user_type": "viewer",
                                     "post_id": str(a.id)})
    assert inside.status_code == 200, inside.text


@pytest.mark.asyncio
async def test_role_update_holder_cannot_extend_their_own_role(client, db, headers):
    """Reproduced: role.update with admin.true added every permission to the caller's own role"""
    await make_admin(db)
    editor = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.update"])
    own_role = await own_role_id(db, editor.id)
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
    editor = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.update", "tag.view", "tag.create"])
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
    creator = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.create"])
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
    archiver = await make_user(db, perms=[ADMIN_TRUE, "role.view", "role.archive", "tag.view"])
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
@pytest.mark.parametrize("change", ["certificate", "is_active", "username", "username_alias", "post_id"])
async def test_limited_admin_cannot_change_a_more_privileged_user(client, db, headers, change):
    post = await make_post(db)
    admin = await make_admin(db)
    admin_id = admin.id
    stored_before = (admin.username, admin.username_alias, admin.post_id, admin.is_active, admin.pki_certificate_fingerprint_hash)
    manager = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.update"])
    assignments_before = await stored_assignments(db, admin_id)
    request = {
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
    assert await stored_assignments(db, admin_id) == assignments_before


@pytest.mark.asyncio
async def test_a_limited_admin_edits_their_own_account(client, db, headers):
    """The caller acting on themselves is not a more privileged target"""
    await make_admin(db)
    manager = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.update"])
    response = await client.patch(USER.format(user_id=manager.id), headers=headers(manager), data={"username_alias": "Me"})
    assert response.status_code == 200, response.text
    assert response.json()["username_alias"] == "Me"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["end_sessions", "end_session"])
async def test_limited_admin_cannot_end_sessions_of_a_more_privileged_user(client, db, headers, route):
    admin = await make_admin(db)
    peer = await make_user(db, perms=["user.view"])
    manager = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.update", "post.view"])
    sessions = {target.id: await AuthService.create_session(db, target) for target in (admin, peer)}

    async def call(target):
        if route == "end_sessions":
            return await client.post(USER_SESSIONS_END.format(user_id=target.id), headers=headers(manager))
        return await client.post(USER_SESSION_END.format(user_id=target.id, session_id=sessions[target.id].id),
                                 headers=headers(manager))

    async def ended(target):
        return (await db.exec(select(UserSession.ended_at).where(UserSession.id == sessions[target.id].id))).one()

    body = problem(await call(admin), 403, "target_user_more_privileged")
    assert "audit.view" in body["missing"]
    assert await ended(admin) is None

    allowed = await call(peer)
    assert allowed.status_code == 200, allowed.text
    assert await ended(peer) is not None


@pytest.mark.asyncio
async def test_limited_admin_cannot_archive_or_restore_a_more_privileged_user(client, db, headers):
    admin = await make_admin(db)
    second_admin = await make_admin(db)
    archiver = await make_user(db, perms=[ADMIN_TRUE, "user.view", "user.archive"])
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


@pytest.mark.asyncio
async def test_target_user_needs_the_routes_permission_covering_them(client, db, headers):
    """Each route checks its own user.* permission at the target's home post: user.update for PATCH and the session
    routes, user.archive for archive and restore"""
    a, b = await make_post(db), await make_post(db)
    await make_admin(db)
    caller = await make_user(db)
    await assign(db, caller, await make_role(db, ["user.view", "user.update"]), Node.post(a.id))
    await assign(db, caller, await make_role(db, ["user.view", "user.archive"]), Node.post(b.id))
    at_a, at_b = await make_user(db, post_id=a.id), await make_user(db, post_id=b.id)

    assert (await client.patch(USER.format(user_id=at_a.id), headers=headers(caller), data={"username_alias": "A"})).status_code == 200
    body = problem(await client.patch(USER.format(user_id=at_b.id), headers=headers(caller), data={"username_alias": "B"}),
                   403, "target_user_more_privileged")
    assert body["missing"] == ["user.update"]
    problem(await client.post(USER_SESSIONS_END.format(user_id=at_b.id), headers=headers(caller)),
            403, "target_user_more_privileged")

    body = problem(await client.post(USER_ARCHIVE.format(user_id=at_a.id), headers=headers(caller)),
                   403, "target_user_more_privileged")
    assert body["missing"] == ["user.archive"]
    assert (await client.post(USER_ARCHIVE.format(user_id=at_b.id), headers=headers(caller))).status_code == 200
    assert (await client.post(USER_RESTORE.format(user_id=at_b.id), headers=headers(caller))).status_code == 200


@pytest.mark.asyncio
async def test_moving_a_user_needs_user_update_covering_the_new_post(client, db, headers):
    a, b = await make_post(db), await make_post(db)
    caller = await make_user(db)
    role = await make_role(db, ["user.view", "user.update"])
    await assign(db, caller, role, Node.post(a.id))
    target = await make_user(db, post_id=a.id)

    body = problem(await client.patch(USER.format(user_id=target.id), headers=headers(caller), data={"post_id": str(b.id)}),
                   403, "permission_required_for_action")
    assert body["permission"] == "user.update" and body["action"] == "move_user"
    assert (await db.exec(select(User.post_id).where(User.id == target.id))).one() == a.id

    await assign(db, caller, role, Node.post(b.id))
    moved = await client.patch(USER.format(user_id=target.id), headers=headers(caller), data={"post_id": str(b.id)})
    assert moved.status_code == 200, moved.text
    assert moved.json()["post_id"] == b.id


@pytest.mark.asyncio
async def test_a_target_with_an_assignment_beyond_the_caller(db):
    """check_target_user: every assignment of the target must be one the caller could grant at its node"""
    a, b = await make_post(db), await make_post(db)
    caller = await make_user(db)
    await assign(db, caller, await make_role(db, ["user.view", "user.update", "observation.view"]), Node.post(a.id))
    target = await make_user(db, perms=["observation.view"], post_id=a.id)
    auth = await load_auth_context(db, caller)
    await check_target_user(db, auth, target)

    await assign(db, target, await make_role(db, ["observation.view"]), Node.post(b.id))
    with pytest.raises(ApiError) as caught:
        await check_target_user(db, auth, target)
    assert caught.value.code is AccessErrorCode.TARGET_USER_MORE_PRIVILEGED
    assert caught.value.params["missing"] == ["observation.view"]


@pytest.mark.asyncio
async def test_an_archived_role_of_the_target_still_counts(client, db, headers):
    """Spec section 6 rule 2: the caller could grant every existing assignment of the target. An assignment of an
    archived role grants nothing today but comes back with the role's restore, so it counts too"""
    a = await make_post(db)
    caller = await make_user(db)
    await assign(db, caller, await make_role(db, ["user.view", "user.update", "observation.view"]), Node.post(a.id))
    target = await make_user(db, post_id=a.id)
    beyond = await make_role(db, ["observation.view", "observation.create"])
    await assign(db, target, beyond, Node.post(a.id))
    beyond.is_deleted = True
    db.add(beyond)
    await db.commit()
    assert (await load_auth_context(db, target)).grants == (), "the archived role grants nothing to the target"

    auth = await load_auth_context(db, caller)
    with pytest.raises(ApiError) as caught:
        await check_target_user(db, auth, target)
    assert caught.value.code is AccessErrorCode.TARGET_USER_MORE_PRIVILEGED
    assert caught.value.params["missing"] == ["observation.create"]
    body = problem(await client.patch(USER.format(user_id=target.id), headers=headers(caller), data={"username_alias": "X"}),
                   403, "target_user_more_privileged")
    assert body["missing"] == ["observation.create"]


# ----------------------------------------------------------------- the last administrator

@pytest.mark.asyncio
async def test_last_admin_is_the_builtin_admin_at_root(db):
    with pytest.raises(ApiError) as caught:
        await check_an_admin_remains(db)
    assert caught.value.code is ErrorCode.LAST_ADMIN_REQUIRED

    post = await make_post(db)
    full = await builtin_role(db)
    every = await make_role(db, await stored_permissions(db, full.id))   # a custom copy: not the administrator
    await make_user(db, role=every)
    await assign(db, await make_user(db, post_id=post.id), full, Node.post(post.id))   # the admin role below the root
    with pytest.raises(ApiError):
        await check_an_admin_remains(db)

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


@pytest.mark.asyncio
async def test_the_only_admin_cannot_drop_their_admin_at_root(db):
    """Reproduced: the only administrator replaced their roles and nobody held admin.true any more. The check the
    assignment API runs after removing an assignment of the admin role at the root: taking it from oneself answers
    cannot_remove_own_admin, the last administrator included (the more specific answer)"""
    admin = await make_admin(db)
    auth = await load_auth_context(db, admin)
    held_before = await holds_admin_at_root(db, admin.id)
    assert held_before
    await db.exec(delete(RoleAssignment).where(RoleAssignment.user_id == admin.id))
    with pytest.raises(ApiError) as caught:
        await check_admin_kept(db, auth, held_before)
    assert caught.value.code is ErrorCode.CANNOT_REMOVE_OWN_ADMIN
    await db.rollback()


@pytest.mark.asyncio
async def test_the_last_admin_role_at_root_cannot_go_by_someone_else(db):
    """A caller who is not the administrator (every permission through a custom role) removing the only
    administrator's admin role at the root: last_admin_required"""
    admin = await make_admin(db)
    every = await make_role(db, await stored_permissions(db, (await builtin_role(db)).id))
    manager = await make_user(db, role=every)
    auth = await load_auth_context(db, manager)
    held_before = await holds_admin_at_root(db, manager.id)
    assert not held_before                     # every permission through a custom role: not the administrator
    await db.exec(delete(RoleAssignment).where(RoleAssignment.user_id == admin.id))
    with pytest.raises(ApiError) as caught:
        await check_admin_kept(db, auth, held_before)
    assert caught.value.code is ErrorCode.LAST_ADMIN_REQUIRED
    await db.rollback()


@pytest.mark.asyncio
async def test_an_admin_cannot_drop_their_own_admin_while_others_remain(db):
    admin = await make_admin(db)
    peer_id = (await make_admin(db)).id
    auth = await load_auth_context(db, admin)
    held_before = await holds_admin_at_root(db, admin.id)

    await db.exec(delete(RoleAssignment).where(RoleAssignment.user_id == admin.id))
    with pytest.raises(ApiError) as caught:
        await check_admin_kept(db, auth, held_before)
    assert caught.value.code is ErrorCode.CANNOT_REMOVE_OWN_ADMIN
    await db.rollback()
    await db.refresh(admin)

    await db.exec(delete(RoleAssignment).where(RoleAssignment.user_id == peer_id))
    await check_admin_kept(db, auth, held_before)   # someone else's: allowed while the caller stays
    await db.rollback()


@pytest.mark.asyncio
async def test_the_administrator_is_the_builtin_admin_role_at_the_root(db):
    """One definition (R27): holds_admin_at_root reads the assignment, by builtin_key; the role's name, a custom copy
    and the admin role below the root make nobody the administrator"""
    post = await make_post(db)
    admin = await make_admin(db)
    full = await builtin_role(db)
    copy = await make_role(db, await stored_permissions(db, full.id), name="admin_copy")
    with_copy = await make_user(db, user_type=UserType.ADMIN)
    await assign(db, with_copy, copy, ROOT)
    below_root = await make_user(db, post_id=post.id, user_type=UserType.ADMIN)
    await assign(db, below_root, full, Node.post(post.id))
    assert await holds_admin_at_root(db, admin.id)
    assert not await holds_admin_at_root(db, with_copy.id)
    assert not await holds_admin_at_root(db, below_root.id)


@pytest.mark.asyncio
async def test_no_role_change_takes_the_administrator_away(client, db, headers):
    """Reproduced: the only administrator archived the role that granted them admin.true. The administrator is the
    built-in admin role at the root: it cannot be archived, stripped or deleted, and a custom role holding every
    permission makes nobody the administrator"""
    admin = await make_admin(db)
    full = await builtin_role(db)
    audit_view = await permission_id(db, "audit.view")

    problem(await client.post(ROLE_ARCHIVE.format(role_id=full.id), headers=headers(admin)), 409, "builtin_role_not_archivable")
    problem(await client.delete(ROLE_PERMISSION.format(role_id=full.id, permission_id=audit_view), headers=headers(admin)),
            409, "builtin_role_permissions_fixed")
    problem(await client.delete(ROLE.format(role_id=full.id), headers=headers(admin)), 409, "builtin_role_not_deletable")

    copy = await make_role(db, await stored_permissions(db, full.id))
    await assign(db, admin, copy, ROOT)
    assert (await client.post(ROLE_ARCHIVE.format(role_id=copy.id), headers=headers(admin))).status_code == 200
    await check_an_admin_remains(db)


# ----------------------------------------------------------------- the default assignment of a new user

@pytest.mark.asyncio
async def test_create_user_makes_default_assignment(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)

    async def create(name: str, **fields) -> int:
        response = await client.post(USERS, headers=headers(admin), files=certificate_file(f"cn-{name}"),
                                     data={"username": name, "username_alias": name.title(), **fields})
        assert response.status_code == 200, response.text
        return response.json()["id"]

    operator, admin_role = await builtin_role(db, "operator"), await builtin_role(db, "admin")
    at_post = await create("op_at_post", user_type="operator", post_id=str(post.id))
    assert await stored_assignments(db, at_post) == {(operator.id, f"p:{post.id}")}
    at_root = await create("op_at_root", user_type="operator")
    assert await stored_assignments(db, at_root) == {(operator.id, "root")}
    # the admin base role holds system permissions: always at the root
    administrator = await create("second_admin", user_type="admin", post_id=str(post.id))
    assert await stored_assignments(db, administrator) == {(admin_role.id, "root")}

    types = dict((await db.exec(select(User.id, User.user_type).where(User.id.in_([at_post, at_root, administrator])))).all())
    assert types == {at_post: "operator", at_root: "operator", administrator: "admin"}
    (row,) = (await db.exec(select(RoleAssignment).where(RoleAssignment.user_id == at_post))).all()
    assert row.scope_post_id == post.id and row.scope_group_id is None and row.created_by == admin.id

    missing = await client.post(USERS, headers=headers(admin), files=certificate_file("cn-typeless"),
                                data={"username": "typeless", "username_alias": "Typeless"})
    assert missing.status_code == 422, missing.text
    assert missing.json()["errors"][0]["pointer"] == "#/user_type"


# ----------------------------------------------------------------- audit of user changes

@pytest.mark.asyncio
async def test_user_audit_records_type_assignment_and_certificate_replacement(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    operator = await builtin_role(db, "operator")

    created = await client.post(USERS, headers=headers(admin), files=certificate_file("cn-audited"),
                                data={"username": "audited", "username_alias": "Audited", "user_type": "operator",
                                      "post_id": str(post.id)})
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]

    async def audit_rows(action):
        rows = await db.exec(select(AuditLog).where(
            AuditLog.resource_type == "user", AuditLog.resource_id == user_id, AuditLog.action == action).order_by(AuditLog.id))
        return rows.all()

    (create_row,) = await audit_rows("create")
    assert create_row.new_values["user_type"] == "operator"
    assert create_row.new_values["assignments"] == [{"role_id": operator.id, "scope": f"p:{post.id}"}]

    changed = await client.patch(USER.format(user_id=user_id), headers=headers(admin), files=certificate_file("cn-audited-new"))
    assert changed.status_code == 200, changed.text
    alias = await client.patch(USER.format(user_id=user_id), headers=headers(admin), data={"username_alias": "Renamed"})
    assert alias.status_code == 200, alias.text

    certificate_change, alias_change = await audit_rows("update")
    assert certificate_change.new_values["certificate_replaced"] is True
    assert alias_change.new_values["certificate_replaced"] is False
    assert alias_change.old_values["username_alias"] == "Audited" and alias_change.new_values["username_alias"] == "Renamed"
