"""Audit writers of authentication and access (part 6, task 3a): one write per module, its event, category, scope and
params as the catalog declares them (enums/audit.py)."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.service import AuthService
from app.backend.enums.audit import AuditCategory, AuditEvent
from app.backend.enums.general import BuiltinRole, SessionRevokeCause, UserType
from app.backend.enums.routes import (
    AuthRoutes, CategoryRoutes, MediaFormatRoutes, RoleRoutes, SignalTypeRoutes, TagRoutes, UserRoutes)
from app.backend.models.audit import AuditLog
from app.backend.models.role import Permission
from tests.helpers import assign, builtin_role, issue, make_admin, make_post, make_user


async def rows_of_event(db, event: AuditEvent) -> list:
    result = await db.exec(select(AuditLog).where(AuditLog.event == event.code).order_by(AuditLog.id)
                           .execution_options(populate_existing=True))
    return result.all()


def check_row(row: AuditLog, event: AuditEvent, scope_key, params: dict) -> None:
    assert row.event == event.code and row.category == event.category.value
    assert row.scope_key == scope_key
    assert row.params == params


# ---------------------------------------------------------------- auth/service.py

async def test_login_and_logout_rows(client, db, headers):
    post = await make_post(db)
    user = await make_user(db, post_id=post.id)
    row = await AuthService.create_session(db, user)
    (login,) = await rows_of_event(db, AuditEvent.USER_LOGIN)
    check_row(login, AuditEvent.USER_LOGIN, f"p:{post.id}", {"username": user.username})
    assert login.category == AuditCategory.AUTHENTICATION.value and login.resource_id == user.id
    assert login.scope_post_id == post.id and login.details is None

    auth_header = {"Authorization": f"Bearer {AuthService.session_tokens(user.id, row)[0]}"}
    assert (await client.post(V1.path(AuthRoutes.LOGOUT), headers=auth_header)).status_code == 200
    (logout,) = await rows_of_event(db, AuditEvent.SESSION_LOGOUT)
    check_row(logout, AuditEvent.SESSION_LOGOUT, f"p:{post.id}", {"username": user.username})
    assert logout.details is None


async def test_sessions_ended_rows(db):
    user = await make_user(db)                       # no home post: the root
    await AuthService.create_session(db, user)
    await AuthService.invalidate_all_user_sessions(db, user.id, SessionRevokeCause.USER_TYPE_CHANGED)
    (ended,) = await rows_of_event(db, AuditEvent.SESSION_END)
    assert ended.category == AuditCategory.ACCESS.value and ended.scope_key == ROOT.key
    assert ended.params == {"username": user.username, "cause": "user_type_changed", "session_count": 1}
    assert ended.details is None and ended.resource_id == user.id


# ---------------------------------------------------------------- api/v1/users.py

async def test_user_writes(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    created = await client.post(V1.path(UserRoutes.ROOT), headers=headers(admin), data={
        "username": "audited", "username_alias": "Audited", "user_type": "operator", "post_id": str(post.id)})
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    (create,) = await rows_of_event(db, AuditEvent.USER_CREATE)
    check_row(create, AuditEvent.USER_CREATE, f"p:{post.id}",
              {"username": "audited", "user_type": "operator"})
    assert create.details is None and create.new_values["user_type"] == "operator"

    updated = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=user_id), headers=headers(admin),
                                 data={"username_alias": "Renamed"})
    assert updated.status_code == 200, updated.text
    (update,) = await rows_of_event(db, AuditEvent.USER_UPDATE)
    check_row(update, AuditEvent.USER_UPDATE, f"p:{post.id}", {"username": "audited"})

    archived = await client.post(V1.path(UserRoutes.USER_ARCHIVE, user_id=user_id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    (archive,) = await rows_of_event(db, AuditEvent.USER_ARCHIVE)
    check_row(archive, AuditEvent.USER_ARCHIVE, f"p:{post.id}", {"username": "audited"})
    restored = await client.post(V1.path(UserRoutes.USER_RESTORE, user_id=user_id), headers=headers(admin))
    assert restored.status_code == 200, restored.text
    (restore,) = await rows_of_event(db, AuditEvent.USER_RESTORE)
    check_row(restore, AuditEvent.USER_RESTORE, f"p:{post.id}", {"username": "audited"})


async def test_force_logout_row(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    target = await make_user(db, post_id=post.id)
    live = await AuthService.create_session(db, target)
    ended = await client.post(V1.path(UserRoutes.USER_SESSION_END, user_id=target.id, session_id=live.id),
                              headers=headers(admin))
    assert ended.status_code == 200, ended.text
    (row,) = await rows_of_event(db, AuditEvent.SESSION_END)
    check_row(row, AuditEvent.SESSION_END, f"p:{post.id}", {
        "username": target.username, "cause": SessionRevokeCause.ENDED_BY_ADMIN.value, "ended_session_id": live.id})
    assert row.user_id == admin.id and row.details is None


async def test_assignment_rows(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    target = await make_user(db, post_id=post.id, user_type=UserType.OPERATOR)
    await assign(db, target, await builtin_role(db, BuiltinRole.OPERATOR), Node.post(post.id))
    editor = await builtin_role(db, BuiltinRole.TAG_EDITOR)
    other = await make_post(db, "Other")
    operator = await builtin_role(db, BuiltinRole.OPERATOR)

    created = await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id), headers=headers(admin),
                                json={"role_id": operator.id, "scope": {"kind": "post", "id": other.id}})
    assert created.status_code == 201, created.text
    (made,) = await rows_of_event(db, AuditEvent.ROLE_ASSIGNMENT_CREATE)
    check_row(made, AuditEvent.ROLE_ASSIGNMENT_CREATE, f"p:{other.id}", {
        "username": target.username, "role_name": operator.name, "node_name": "Other"})
    assert made.details is None and made.new_values["user_id"] == target.id

    deleted = await client.delete(V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=target.id,
                                          assignment_id=created.json()["id"]), headers=headers(admin))
    assert deleted.status_code == 204, deleted.text
    (removed,) = await rows_of_event(db, AuditEvent.ROLE_ASSIGNMENT_DELETE)
    check_row(removed, AuditEvent.ROLE_ASSIGNMENT_DELETE, f"p:{other.id}", {
        "username": target.username, "role_name": operator.name, "node_name": "Other"})

    system = await client.post(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENTS, user_id=target.id),
                               headers=headers(admin), json={"role_id": editor.id})
    assert system.status_code == 201, system.text
    (given,) = await rows_of_event(db, AuditEvent.SYSTEM_ROLE_ASSIGNMENT_CREATE)
    check_row(given, AuditEvent.SYSTEM_ROLE_ASSIGNMENT_CREATE, None,
              {"username": target.username, "role_name": editor.name})
    taken = await client.delete(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENT_BY_ID, user_id=target.id,
                                        assignment_id=system.json()["id"]), headers=headers(admin))
    assert taken.status_code == 204, taken.text
    (gone,) = await rows_of_event(db, AuditEvent.SYSTEM_ROLE_ASSIGNMENT_DELETE)
    check_row(gone, AuditEvent.SYSTEM_ROLE_ASSIGNMENT_DELETE, None,
              {"username": target.username, "role_name": editor.name})


async def test_root_assignment_names_the_root(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.OPERATOR)
    operator = await builtin_role(db, BuiltinRole.OPERATOR)
    await assign(db, target, operator, Node.post((await make_post(db)).id))
    created = await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id), headers=headers(admin),
                                json={"role_id": operator.id, "scope": {"kind": "root"}})
    assert created.status_code == 201, created.text
    (made,) = await rows_of_event(db, AuditEvent.ROLE_ASSIGNMENT_CREATE)
    assert made.scope_key == ROOT.key and made.params["node_name"] == "root"


# ---------------------------------------------------------------- api/v1/user_certificates.py

async def test_certificate_rows(client, db, headers, pki):
    post = await make_post(db)
    admin = await make_admin(db)
    user = await make_user(db, post_id=post.id)
    issued = await issue(client, headers, admin, user)
    assert issued.status_code == 201, issued.text
    (row,) = await rows_of_event(db, AuditEvent.USER_CERTIFICATE_ISSUE)
    assert row.category == AuditCategory.ACCESS.value and row.scope_key == f"p:{post.id}"
    assert set(row.params) == {"username", "label", "serial", "sha256_fingerprint", "not_after"}
    assert row.params["username"] == user.username and row.params["label"] == "Laptop"
    assert row.details is None and isinstance(row.params["not_after"], str)

    exported = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_EXPORT, user_id=user.id,
                                        certificate_id=issued.json()["certificate"]["id"]), headers=headers(admin))
    assert exported.status_code == 200, exported.text
    (exp,) = await rows_of_event(db, AuditEvent.USER_CERTIFICATE_EXPORT)
    assert exp.params["export_count"] == 1 and "session_count" not in exp.params

    revoked = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_REVOKE, user_id=user.id,
                                        certificate_id=issued.json()["certificate"]["id"]),
                                json={"reason": "key_compromise"}, headers=headers(admin))
    assert revoked.status_code == 200, revoked.text
    (gone,) = await rows_of_event(db, AuditEvent.USER_CERTIFICATE_REVOKE)
    assert gone.params["reason"] == "key_compromise" and gone.scope_key == f"p:{post.id}"
    assert gone.params["session_count"] == 0 and "export_count" not in gone.params


# ---------------------------------------------------------------- cli

async def test_cli_rows(db, session_maker, monkeypatch, tmp_path, pki):
    from app.backend.cli import init_user
    from app.backend.cli import pki as pki_cli

    async def nothing():
        return None

    async def maker():
        return session_maker

    monkeypatch.setattr(init_user, "create_database_if_not_exists", nothing)
    monkeypatch.setattr(init_user, "get_session_maker", maker)
    await init_user.create_user("cli_admin", "CLI Admin", None, UserType.SYSTEM_ADMINISTRATOR)
    (created,) = await rows_of_event(db, AuditEvent.USER_CREATE)
    check_row(created, AuditEvent.USER_CREATE, ROOT.key,
              {"username": "cli_admin", "user_type": "system_administrator", "via": "cli"})

    monkeypatch.setattr(pki_cli, "get_session_maker", maker)

    async def close_db():
        return None

    monkeypatch.setattr(pki_cli, "close_db", close_db)
    post = await make_post(db)
    user = await make_user(db, post_id=post.id)
    assert await pki_cli.run(["issue", "--username", user.username, "--label", "Laptop",
                              "--out", str(tmp_path / "out")]) == 0
    (issued,) = await rows_of_event(db, AuditEvent.USER_CERTIFICATE_ISSUE)
    assert issued.scope_key == f"p:{post.id}" and issued.params["via"] == "cli"
    assert issued.params["username"] == user.username and issued.user_id is None


# ---------------------------------------------------------------- api/v1/roles.py (part 3b)

async def test_role_writes(client, db, headers):
    admin = await make_admin(db)
    h = headers(admin)
    created = await client.post(V1.path(RoleRoutes.ROOT), headers=h, json={
        "name": "night_watch", "display_name": "Night watch", "kind": "tree"})
    assert created.status_code == 201, created.text
    role_id = created.json()["id"]
    (row,) = await rows_of_event(db, AuditEvent.ROLE_CREATE)
    check_row(row, AuditEvent.ROLE_CREATE, None, {"role_name": "night_watch", "role_kind": "tree"})
    assert row.category == AuditCategory.ACCESS.value and row.details is None and row.resource_id == role_id
    assert row.new_values["name"] == "night_watch" and row.user_id == admin.id

    url = V1.path(RoleRoutes.ROLE_BY_ID, role_id=role_id)
    assert (await client.patch(url, headers=h, json={"description": "note"})).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent.ROLE_UPDATE)
    check_row(row, AuditEvent.ROLE_UPDATE, None, {"role_name": "night_watch", "role_kind": "tree"})
    assert row.details is None

    permission = (await db.exec(select(Permission).where(Permission.name == "tag.view"))).one()
    perm_url = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role_id, permission_id=permission.id)
    assert (await client.put(perm_url, headers=h)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent.ROLE_PERMISSION_ADD)
    check_row(row, AuditEvent.ROLE_PERMISSION_ADD, None, {
        "role_name": "night_watch", "role_kind": "tree", "permission": "tag.view"})
    assert row.new_values == {"permission_added": "tag.view"} and row.details is None
    assert (await client.delete(perm_url, headers=h)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent.ROLE_PERMISSION_REMOVE)
    check_row(row, AuditEvent.ROLE_PERMISSION_REMOVE, None, {
        "role_name": "night_watch", "role_kind": "tree", "permission": "tag.view"})
    assert row.old_values == {"permission_removed": "tag.view"}

    clone = await client.post(V1.path(RoleRoutes.ROLE_CLONE, role_id=role_id), headers=h, json={
        "name": "night_copy", "display_name": "Night copy"})
    assert clone.status_code == 201, clone.text
    (row,) = await rows_of_event(db, AuditEvent.ROLE_CLONE)
    check_row(row, AuditEvent.ROLE_CLONE, None, {
        "role_name": "night_copy", "role_kind": "tree", "cloned_from": "night_watch",
        "cloned_from_id": role_id})
    assert row.details is None and row.resource_id == clone.json()["id"]

    assert (await client.post(V1.path(RoleRoutes.ROLE_ARCHIVE, role_id=role_id), headers=h)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent.ROLE_ARCHIVE)
    check_row(row, AuditEvent.ROLE_ARCHIVE, None, {"role_name": "night_watch", "role_kind": "tree"})
    assert row.old_values["is_deleted"] is False and row.new_values["is_deleted"] is True
    assert (await client.post(V1.path(RoleRoutes.ROLE_RESTORE, role_id=role_id), headers=h)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent.ROLE_RESTORE)
    check_row(row, AuditEvent.ROLE_RESTORE, None, {"role_name": "night_watch", "role_kind": "tree"})

    assert (await client.post(V1.path(RoleRoutes.ROLE_ARCHIVE, role_id=role_id), headers=h)).status_code == 200
    assert (await client.delete(url, headers=h)).status_code == 204
    (row,) = await rows_of_event(db, AuditEvent.ROLE_DELETE)
    check_row(row, AuditEvent.ROLE_DELETE, None, {"role_name": "night_watch", "role_kind": "tree"})
    assert row.details is None and row.old_values["name"] == "night_watch" and "assignments" in row.old_values


# ---------------------------------------------------------------- dictionaries (part 3b)

async def check_lifecycle(client, db, h, base, by_id, archive, restore, create_body, update_body, prefix, params):
    created = await client.post(base, headers=h, json=create_body)
    assert created.status_code in (200, 201), created.text
    item_id = created.json()["id"]
    (row,) = await rows_of_event(db, AuditEvent[f"{prefix}_CREATE"])
    check_row(row, AuditEvent[f"{prefix}_CREATE"], None, params)
    assert row.category == AuditCategory.DICTIONARIES.value and row.details is None
    assert row.resource_id == item_id and row.old_values is None and row.new_values["id"] == item_id

    assert (await client.put(by_id(item_id), headers=h, json=update_body)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent[f"{prefix}_UPDATE"])
    check_row(row, AuditEvent[f"{prefix}_UPDATE"], None, params)
    assert row.details is None and row.old_values is not None

    assert (await client.post(archive(item_id), headers=h)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent[f"{prefix}_ARCHIVE"])
    check_row(row, AuditEvent[f"{prefix}_ARCHIVE"], None, params)
    assert row.new_values["is_deleted"] is True and row.details is None
    assert (await client.post(restore(item_id), headers=h)).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent[f"{prefix}_RESTORE"])
    check_row(row, AuditEvent[f"{prefix}_RESTORE"], None, params)
    assert row.new_values["is_deleted"] is False
    return item_id


async def test_category_writes(client, db, headers):
    h = headers(await make_admin(db))
    await check_lifecycle(
        client, db, h, V1.path(CategoryRoutes.CREATE),
        lambda i: V1.path(CategoryRoutes.CATEGORY_BY_ID, category_id=i),
        lambda i: V1.path(CategoryRoutes.CATEGORY_ARCHIVE, category_id=i),
        lambda i: V1.path(CategoryRoutes.CATEGORY_RESTORE, category_id=i),
        {"name": "Radar", "alias": "radar"}, {"alias": "radar2"}, "CATEGORY", {"name": "Radar"})


async def test_tag_writes(client, db, headers):
    h = headers(await make_admin(db))
    await check_lifecycle(
        client, db, h, V1.path(TagRoutes.CREATE),
        lambda i: V1.path(TagRoutes.TAG_BY_ID, tag_id=i),
        lambda i: V1.path(TagRoutes.TAG_ARCHIVE, tag_id=i),
        lambda i: V1.path(TagRoutes.TAG_RESTORE, tag_id=i),
        {"name": "urgent"}, {"name": "urgent"}, "TAG", {"name": "urgent"})


async def test_signal_type_writes(client, db, headers):
    h = headers(await make_admin(db))
    await check_lifecycle(
        client, db, h, V1.path(SignalTypeRoutes.CREATE),
        lambda i: V1.path(SignalTypeRoutes.SIGNAL_TYPE_BY_ID, signal_type_id=i),
        lambda i: V1.path(SignalTypeRoutes.SIGNAL_TYPE_ARCHIVE, signal_type_id=i),
        lambda i: V1.path(SignalTypeRoutes.SIGNAL_TYPE_RESTORE, signal_type_id=i),
        {"code": "LTE", "name": "LTE uplink"}, {"name": "LTE uplink 2"}, "SIGNAL_TYPE", {"signal_type_code": "LTE"})


async def test_media_format_writes(client, db, headers):
    h = headers(await make_admin(db))
    body = {"media_type": "video", "content_type": "video/x-test", "label": "Test", "extensions": [".tst"],
            "max_file_size_bytes": 1000}
    item_id = await check_lifecycle(
        client, db, h, V1.path(MediaFormatRoutes.CREATE),
        lambda i: V1.path(MediaFormatRoutes.FORMAT_BY_ID, format_id=i),
        lambda i: V1.path(MediaFormatRoutes.FORMAT_ARCHIVE, format_id=i),
        lambda i: V1.path(MediaFormatRoutes.FORMAT_RESTORE, format_id=i),
        body, {"label": "Test 2"}, "MEDIA_FORMAT", {"content_type": "video/x-test"})

    # create over an archived content type restores it: the restore event (second row)
    assert (await client.post(V1.path(MediaFormatRoutes.FORMAT_ARCHIVE, format_id=item_id), headers=h)).status_code == 200
    again = await client.post(V1.path(MediaFormatRoutes.CREATE), headers=h, json=body)
    assert again.status_code == 200, again.text
    restores = await rows_of_event(db, AuditEvent.MEDIA_FORMAT_RESTORE)
    assert len(restores) == 2 and restores[-1].params == {"content_type": "video/x-test"}
    assert restores[-1].old_values["is_deleted"] is True and restores[-1].details is None
