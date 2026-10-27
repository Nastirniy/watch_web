"""Audit writers of authentication and access (part 6, task 3a): one write per module, its event, category, scope and
params as the catalog declares them (enums/audit.py)."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.service import AuthService
from app.backend.enums.audit import AuditCategory, AuditEvent
from app.backend.enums.general import BuiltinRole, SessionRevokeCause, UserType
from app.backend.enums.routes import (
    AuthRoutes, BrigadeRoutes, SearchRoutes, CategoryRoutes, CorpsRoutes, MediaFormatRoutes, MediaRoutes, ObservationRoutes, PostRoutes, RoleRoutes,
    ScopeGroupRoutes, SignalTypeRoutes, TagRoutes, UserRoutes)
from app.backend.config import settings
from app.backend.models.audit import AuditLog
from app.backend.models.observation import SignalType
from app.backend.services.media.manager import reset_media_manager
from app.backend.models.role import Permission
from app.backend.services.unit_history import fill_unit_history
from tests.helpers import (
    assign, builtin_role, issue, make_admin, make_certificate, make_certificate_row, nginx_headers, make_dictionary,
    make_group, make_media, make_observation, make_post, make_user, observation_payload)


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


# ---------------------------------------------------------------- posts, groups and units (part 3c)

async def test_post_writes(client, db, headers):
    h = headers(await make_admin(db))
    created = await client.post(V1.path(PostRoutes.ROOT), headers=h, json={"name": "Alpha", "code": "AUD1"})
    assert created.status_code == 200, created.text
    post_id = created.json()["id"]
    node = f"p:{post_id}"
    (create,) = await rows_of_event(db, AuditEvent.POST_CREATE)
    check_row(create, AuditEvent.POST_CREATE, node, {"post_name": "Alpha", "post_code": "AUD1"})
    assert create.category == AuditCategory.STRUCTURE.value and create.resource_id == post_id
    assert create.scope_post_id == post_id and create.details is None and create.new_values["name"] == "Alpha"

    updated = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post_id), headers=h, json={"name": "Beta"})
    assert updated.status_code == 200, updated.text
    (update,) = await rows_of_event(db, AuditEvent.POST_UPDATE)
    check_row(update, AuditEvent.POST_UPDATE, node, {"post_name": "Beta", "post_code": "AUD1"})
    assert update.old_values["name"] == "Alpha" and update.details is None

    assert (await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post_id), headers=h)).status_code == 200
    assert (await client.post(V1.path(PostRoutes.POST_RESTORE, post_id=post_id), headers=h)).status_code == 200
    (archive,) = await rows_of_event(db, AuditEvent.POST_ARCHIVE)
    (restore,) = await rows_of_event(db, AuditEvent.POST_RESTORE)
    check_row(archive, AuditEvent.POST_ARCHIVE, node, {"post_name": "Beta", "post_code": "AUD1"})
    check_row(restore, AuditEvent.POST_RESTORE, node, {"post_name": "Beta", "post_code": "AUD1"})
    assert archive.details is None and restore.details is None


async def test_scope_group_writes(client, db, headers):
    h = headers(await make_admin(db))
    created = await client.post(V1.path(ScopeGroupRoutes.ROOT), headers=h, json={"name": "East"})
    assert created.status_code == 200, created.text
    group_id = created.json()["id"]
    node = f"g:{group_id}"
    (create,) = await rows_of_event(db, AuditEvent.SCOPE_GROUP_CREATE)
    check_row(create, AuditEvent.SCOPE_GROUP_CREATE, node, {"group_name": "East", "group_kind": "group"})
    assert create.scope_group_id == group_id and create.resource_id == group_id and create.details is None

    corps = await client.post(V1.path(CorpsRoutes.ROOT), headers=h, json={"name": "Corps A"})
    assert corps.status_code == 201, corps.text
    corps_id = corps.json()["id"]
    renamed = await client.patch(V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=corps_id), headers=h,
                                 json={"name": "Corps B"})
    assert renamed.status_code == 200, renamed.text
    (update,) = await rows_of_event(db, AuditEvent.SCOPE_GROUP_UPDATE)
    check_row(update, AuditEvent.SCOPE_GROUP_UPDATE, f"g:{corps_id}", {"group_name": "Corps B", "group_kind": "corps"})
    assert update.old_values["name"] == "Corps A" and update.details is None

    assert (await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=group_id), headers=h)).status_code == 200
    assert (await client.post(V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=group_id), headers=h)).status_code == 200
    (archive,) = await rows_of_event(db, AuditEvent.SCOPE_GROUP_ARCHIVE)
    (restore,) = await rows_of_event(db, AuditEvent.SCOPE_GROUP_RESTORE)
    check_row(archive, AuditEvent.SCOPE_GROUP_ARCHIVE, node, {"group_name": "East", "group_kind": "group"})
    check_row(restore, AuditEvent.SCOPE_GROUP_RESTORE, node, {"group_name": "East", "group_kind": "group"})


async def test_unit_archive_cascade_rows(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, name="Corps 1", kind="corps")
    brigade = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    in_brigade = await make_post(db, group_id=brigade.id)
    direct = await make_post(db, group_id=corps.id)
    corps_id, brigade_id = corps.id, brigade.id
    archived = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps_id), headers=h,
                                 params={"cascade": "true"})
    assert archived.status_code == 200, archived.text

    posts = await rows_of_event(db, AuditEvent.POST_ARCHIVE)
    assert [r.resource_id for r in posts] == [in_brigade.id, direct.id]
    for row, post in zip(posts, (in_brigade, direct)):
        check_row(row, AuditEvent.POST_ARCHIVE, f"p:{post.id}",
                  {"post_name": post.name, "post_code": post.code, "cascade_from": corps_id})
        assert row.details is None
    groups = await rows_of_event(db, AuditEvent.SCOPE_GROUP_ARCHIVE)
    assert [r.resource_id for r in groups] == [brigade_id, corps_id]
    check_row(groups[0], AuditEvent.SCOPE_GROUP_ARCHIVE, f"g:{brigade_id}",
              {"group_name": "Brigade 10", "group_kind": "brigade", "cascade_from": corps_id})
    check_row(groups[1], AuditEvent.SCOPE_GROUP_ARCHIVE, f"g:{corps_id}",
              {"group_name": "Corps 1", "group_kind": "corps"})      # the unit itself: not cascaded
    assert all(r.details is None for r in groups)

    # without cascade the posts move out of the unit: post.update rows that name the unit
    other = await make_group(db, name="Corps 2", kind="corps")
    moved = await make_post(db, group_id=other.id)
    other_id = other.id
    assert (await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=other_id), headers=h)).status_code == 200
    (update,) = await rows_of_event(db, AuditEvent.POST_UPDATE)
    check_row(update, AuditEvent.POST_UPDATE, f"p:{moved.id}",
              {"post_name": moved.name, "post_code": moved.code, "cascade_from": other_id})


async def test_fill_history_row(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, name="Corps 1", kind="corps")
    brigade = await make_group(db, name="Brigade 1", parent_id=corps.id, kind="brigade")
    post = await make_post(db)
    for _ in range(2):
        created = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post.id, ids["signal_type_id"]),
                                    headers=h)
        assert created.status_code == 200, created.text
    moved = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": brigade.id}, headers=h)
    assert moved.status_code == 200, moved.text

    await fill_unit_history(db)
    (row,) = await rows_of_event(db, AuditEvent.OBSERVATION_UNITS_FILL_HISTORY)
    check_row(row, AuditEvent.OBSERVATION_UNITS_FILL_HISTORY, None, {"observation_count": 2, "row_count": 4})
    assert row.category == AuditCategory.STRUCTURE.value and row.user_id is None and row.details is None
    assert row.new_values == {"units": {str(brigade.id): 2, str(corps.id): 2}, "observations": 2, "rows": 4}


# ---------------------------------------------------------------- observations, frequencies, media (part 3d)

async def test_observation_writes(client, db, headers):
    post = await make_post(db, name="North")
    ids = await make_dictionary(db)
    h = headers(await make_admin(db))
    created = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post.id, ids["signal_type_id"]),
                                headers=h)
    assert created.status_code == 200, created.text
    obs_id = created.json()["id"]
    signal_code = (await db.get(SignalType, ids["signal_type_id"])).code
    params = {"post_name": "North", "observation_id": obs_id, "signal_type_code": signal_code}
    node = f"p:{post.id}"

    (create,) = await rows_of_event(db, AuditEvent.OBSERVATION_CREATE)
    check_row(create, AuditEvent.OBSERVATION_CREATE, node, params)
    assert create.category == AuditCategory.DATA.value and create.resource_id == obs_id and create.details is None
    assert create.new_values["frequencies"]

    base = V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=obs_id)
    assert (await client.put(base, json={"comment": "changed"}, headers=h)).status_code == 200
    (update,) = await rows_of_event(db, AuditEvent.OBSERVATION_UPDATE)
    check_row(update, AuditEvent.OBSERVATION_UPDATE, node, params)
    assert update.old_values is not None and update.details is None

    disappeared = await client.post(V1.path(ObservationRoutes.MARK_DISAPPEARED, observation_id=obs_id), headers=h,
                                    json={"disappeared_at": datetime.now(timezone.utc).isoformat()})
    assert disappeared.status_code == 200, disappeared.text
    (gone,) = await rows_of_event(db, AuditEvent.OBSERVATION_DISAPPEAR)
    check_row(gone, AuditEvent.OBSERVATION_DISAPPEAR, node, params)

    assert (await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=obs_id), headers=h)).status_code == 200
    assert (await client.post(V1.path(ObservationRoutes.OBSERVATION_RESTORE, observation_id=obs_id), headers=h)).status_code == 200
    (archive,) = await rows_of_event(db, AuditEvent.OBSERVATION_ARCHIVE)
    (restore,) = await rows_of_event(db, AuditEvent.OBSERVATION_RESTORE)
    check_row(archive, AuditEvent.OBSERVATION_ARCHIVE, node, params)
    check_row(restore, AuditEvent.OBSERVATION_RESTORE, node, params)
    assert archive.details is None and restore.details is None


async def test_frequency_writes(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_admin(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    obs_id = observation.id
    h = headers(user)
    node, params = f"p:{post.id}", {"observation_id": obs_id, "frequency_kind": "rc"}

    added = await client.post(V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES, observation_id=obs_id), headers=h,
                              json={"kind": "rc", "frequency_min": "300", "frequency_max": "400"})
    assert added.status_code == 200, added.text
    band_id = added.json()["id"]
    band = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID, observation_id=obs_id, frequency_id=band_id)
    assert (await client.put(band, headers=h, json={"frequency_max": "450"})).status_code == 200
    for route in (ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE, ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE):
        done = await client.post(V1.path(route, observation_id=obs_id, frequency_id=band_id), headers=h)
        assert done.status_code == 200, done.text
    for event in (AuditEvent.OBSERVATION_FREQUENCY_CREATE, AuditEvent.OBSERVATION_FREQUENCY_UPDATE,
                  AuditEvent.OBSERVATION_FREQUENCY_ARCHIVE, AuditEvent.OBSERVATION_FREQUENCY_RESTORE):
        (row,) = await rows_of_event(db, event)
        check_row(row, event, node, params)
        assert row.category == AuditCategory.DATA.value and row.resource_id == band_id and row.details is None


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "_tmp"))
    reset_media_manager()
    yield tmp_path
    reset_media_manager()


async def test_media_writes(client, db, headers, storage):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "media.create", "media.archive", "observation.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    obs_id, node, h = observation.id, f"p:{post.id}", headers(user)

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24)).save(buffer, format="PNG")
    uploaded = await client.post(V1.path(MediaRoutes.UPLOAD), headers=h, data={"observation_id": obs_id, "media_type": "image"},
                                 files={"file": ("shot.png", buffer.getvalue(), "image/png")})
    assert uploaded.status_code in (200, 201), uploaded.text
    (create,) = await rows_of_event(db, AuditEvent.MEDIA_CREATE)
    media_id = create.resource_id
    check_row(create, AuditEvent.MEDIA_CREATE, node, {"observation_id": obs_id, "file_name": "shot.png", "media_type": "image",
                                                                  "via": "multipart"})
    assert create.category == AuditCategory.DATA.value and create.details is None and create.new_values["id"] == media_id

    for route in (MediaRoutes.MEDIA_ARCHIVE, MediaRoutes.MEDIA_RESTORE):
        assert (await client.post(V1.path(route, media_id=media_id), headers=h)).status_code == 200
    (archive,) = await rows_of_event(db, AuditEvent.MEDIA_ARCHIVE)
    (restore,) = await rows_of_event(db, AuditEvent.MEDIA_RESTORE)
    check_row(archive, AuditEvent.MEDIA_ARCHIVE, node, {"observation_id": obs_id})
    check_row(restore, AuditEvent.MEDIA_RESTORE, node, {"observation_id": obs_id})
    assert archive.details is None and archive.resource_id == media_id

    shared = await client.post(V1.path(MediaRoutes.MEDIA_SHARE, media_id=media_id), headers=h, json={"ttl_seconds": 600})
    assert shared.status_code == 200, shared.text
    (share,) = await rows_of_event(db, AuditEvent.MEDIA_SHARE)
    check_row(share, AuditEvent.MEDIA_SHARE, node, {"observation_id": obs_id, "ttl_seconds": 600})
    assert share.details is None and share.resource_id == media_id
    assert share.new_values["size"] and share.new_values["format"] and share.new_values["expires_at"]


async def test_upload_session_writes(client, db, headers, storage):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "media.create"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    h = headers(user)
    created = await client.post(V1.path(MediaRoutes.UPLOADS), headers=h, json={
        "observation_id": observation.id, "media_type": "image", "original_filename": "photo.png",
        "expected_size": 100, "content_type": "image/png"})
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    (create,) = await rows_of_event(db, AuditEvent.UPLOAD_SESSION_CREATE)
    check_row(create, AuditEvent.UPLOAD_SESSION_CREATE, f"p:{post.id}", {"upload_id": upload_id, "media_type": "image", "observation_id": observation.id,
                                                                    "file_name": "photo.png"})
    assert create.resource_id is None and create.details is None and create.new_values["upload_id"] == upload_id

    assert (await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=h)).status_code == 200
    (abort,) = await rows_of_event(db, AuditEvent.UPLOAD_SESSION_ABORT)
    check_row(abort, AuditEvent.UPLOAD_SESSION_ABORT, f"p:{post.id}", {"upload_id": upload_id, "media_type": "image", "observation_id": observation.id,
                                                                    "file_name": "photo.png"})
    assert abort.resource_id is None and abort.details is None


async def test_media_without_an_observation_is_written_at_the_root(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    admin = await make_admin(db)
    media = await make_media(db, await make_observation(db, admin, post, ids["signal_type_id"]), admin)
    media.observation_id = None
    db.add(media)
    await db.commit()
    done = await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media.id), headers=headers(admin))
    assert done.status_code == 200, done.text
    (row,) = await rows_of_event(db, AuditEvent.MEDIA_ARCHIVE)
    check_row(row, AuditEvent.MEDIA_ARCHIVE, ROOT.key, {})


# ---------------------------------------------------------------- part 3e: searches, reconciles

async def test_search_row(client, db, headers):
    user = await make_admin(db)
    assert (await client.get(V1.path(SearchRoutes.TAGS), headers=headers(user))).status_code == 200
    (row,) = await rows_of_event(db, AuditEvent.SEARCH_TAGS)
    check_row(row, AuditEvent.SEARCH_TAGS, None, {"result_count": 0})
    assert row.category == AuditCategory.READS.value and (row.action, row.resource_type) == ("search", "tag")
    assert row.details is None and row.user_id == user.id
    assert row.new_values["results"] == 0 and "params" in row.new_values


async def test_media_posters_reconcile_row(client, db, headers):
    admin = await make_admin(db)
    response = await client.post(V1.path(MediaRoutes.POSTERS_RECONCILE), json={"retry_failed": False}, headers=headers(admin))
    assert response.status_code == 200, response.text
    (row,) = await rows_of_event(db, AuditEvent.MEDIA_POSTERS_RECONCILE)
    check_row(row, AuditEvent.MEDIA_POSTERS_RECONCILE, None, {
        "failed_abandoned": 0, "queued_untracked": 0, "requeued_missing": 0, "retried_failed": 0})
    assert row.category == AuditCategory.SYSTEM.value and row.details is None and row.user_id == admin.id


async def test_database_reconcile_row(db, tmp_path):
    from app.backend.cli.restore_reconcile import run
    media = tmp_path / "uploads"
    (media / "f").mkdir(parents=True)
    (media / "f" / "8_orphan.jpg").write_bytes(b"x")
    import os
    old = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc).timestamp()
    os.utime(media / "f" / "8_orphan.jpg", (old, old))
    assert await run(db, apply=True, upload_path=media, store_dir=None,
                     now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)) == 0
    (row,) = await rows_of_event(db, AuditEvent.DATABASE_RECONCILE)
    check_row(row, AuditEvent.DATABASE_RECONCILE, None, {
        "orphan_files": 1, "stale_derived": 0, "returned_originals": 0, "missing_originals": 0,
        "orphan_packages": 0, "failed": 0})
    assert row.category == AuditCategory.SYSTEM.value and row.details is None


# ---------------------------------------------------------------- auth/service.py: certificate login attempts

async def test_certificate_login_writes_one_row_at_the_end(client, db, headers, production):
    post = await make_post(db)
    user = await make_user(db, post_id=post.id)
    cert = make_certificate("cn-audit-login", 0x7A01)
    row = await make_certificate_row(db, user, cert=cert)

    ok = await client.post(V1.path(AuthRoutes.LOGIN), headers=nginx_headers(cert))
    assert ok.status_code == 200, ok.text
    (attempt,) = await rows_of_event(db, AuditEvent.AUTH_CERTIFICATE_LOGIN)
    check_row(attempt, AuditEvent.AUTH_CERTIFICATE_LOGIN, f"p:{post.id}", {
        "certificate_cn": "cn-audit-login", "certificate_serial": "7A01", "matched": True, "certificate_id": row.id,
        "certificate_fingerprint_prefix": attempt.params["certificate_fingerprint_prefix"]})
    assert len(attempt.params["certificate_fingerprint_prefix"]) == 16
    assert attempt.success is True and attempt.user_id == user.id and attempt.details is None

    stranger = make_certificate("cn-audit-unknown", 0x7A02)
    refused = await client.post(V1.path(AuthRoutes.LOGIN), headers=nginx_headers(stranger))
    assert refused.status_code == 401
    unmatched = (await rows_of_event(db, AuditEvent.AUTH_CERTIFICATE_LOGIN))[1:]
    (miss,) = unmatched
    assert miss.success is False and miss.user_id is None and miss.scope_key is None
    assert miss.params["matched"] is False and miss.params["certificate_id"] is None


@pytest.mark.parametrize("refusal", ["revoked", "expired", "archived", "deactivated"])
async def test_refused_certificate_login_writes_one_row(client, db, production, refusal):
    post = await make_post(db)
    user = await make_user(db, post_id=post.id)
    cert = make_certificate(f"cn-refused-{refusal}", 0x7B00 + len(refusal))
    values = {}
    if refusal == "revoked":
        values = {"revoked_at": datetime.now(timezone.utc), "revocation_reason": "key_compromise"}
    elif refusal == "expired":
        values = {"not_after": datetime.now(timezone.utc) - timedelta(days=1)}
    row = await make_certificate_row(db, user, cert=cert, **values)
    if refusal == "archived":
        user.is_deleted = True
    elif refusal == "deactivated":
        user.is_active = False
    db.add(user)
    await db.commit()

    response = await client.post(V1.path(AuthRoutes.LOGIN), headers=nginx_headers(cert))
    assert response.status_code == 401, response.text
    (attempt,) = await rows_of_event(db, AuditEvent.AUTH_CERTIFICATE_LOGIN)
    assert attempt.success is False and attempt.user_id == user.id and attempt.scope_key == f"p:{post.id}"
    assert attempt.params["matched"] is True and attempt.params["certificate_id"] == row.id
    assert attempt.params["certificate_cn"] == f"cn-refused-{refusal}"
