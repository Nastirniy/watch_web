"""A write route commits its change and its audit row in ONE transaction.

Each route used to commit the change and write the audit row in a second transaction: a failure there (on MariaDB the
audit row's foreign key check of the caller's users row answers 1020 after the refresh opened a read view; a lock
wait timeout; a lost connection) answered 500 for a stored change and lost the audit row.

Add a route to WRITES (one Case): a route module name, how to build the scene, the request and what to read back.
Every Case runs two tests: a failing create_audit_log stores nothing, and the happy path stores one audit row."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import pytest
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import (
    BrigadeRoutes, CategoryRoutes, CorpsRoutes, MediaFormatRoutes, PostRoutes, RoleRoutes, ScopeGroupRoutes,
    SignalTypeRoutes, TagRoutes, UserRoutes,
)
from app.backend.models.audit import AuditLog
from app.backend.models.media_format import MediaFormat
from app.backend.models.observation import Category, SignalType, Tag
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user_certificate import UserCertificate
from app.backend.models.user import User
from tests.helpers import make_admin, make_dictionary, make_group, make_post, make_role
from tests.test_username_validation import make_certificate


async def _audit_fails(*args, **kwargs):
    raise OperationalError("INSERT INTO audit_logs ...", {}, Exception("(1020, 'Record has changed since last read')"))


@dataclass
class Case:
    """module: api.v1 module whose create_audit_log is replaced (a name with a dot is a module path under app.backend:
    "api.shared.helpers.scope_group_helper" for the routes that audit through audit_group); model/resource_type/action: what is stored;
    request(client, headers, user, ids) -> response; ids: the rows of the scene by name;
    target: None for a create (the rows of model are counted), else the name in ids of the row an update changes;
    row_id(ids, body) -> id of the audited row, only when it is not body["id"] (new_values then is no row dump);
    status: the success status of the route"""
    module: str
    model: Any
    resource_type: str
    action: str
    request: Callable[..., Awaitable[Any]]
    target: Optional[str] = None
    row_id: Optional[Callable[[Dict[str, Any], Any], int]] = None
    status: int = 200


async def _scene(db) -> Dict[str, Any]:
    post = await make_post(db)
    user = await make_admin(db, post_id=post.id)
    ids = await make_dictionary(db)
    other = await make_dictionary(db)
    fmt = MediaFormat(media_type="image", content_type="image/x-test", label="Test", extensions=[".xtest"],
                      aliases=[], max_file_size_bytes=1024)
    db.add(fmt)
    await db.commit()
    await db.refresh(fmt)
    role = await make_role(db, ["tag.view"])
    empty_role = await make_role(db, [])
    group = await make_group(db)
    other_group = await make_group(db)
    corps = await make_group(db, kind="corps")
    other_corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    permissions = {p.name: p.id for p in (await db.exec(select(Permission).where(
        Permission.name.in_(["tag.view", "tag.create"])))).all()}
    return {"user": user, **ids, "other_category_id": other["category_id"], "format_id": fmt.id,
            "post_id": (await make_post(db)).id, "role_id": role.id, "empty_role_id": empty_role.id,
            "view_permission_id": permissions["tag.view"], "create_permission_id": permissions["tag.create"],
            "group_id": group.id, "other_group_id": other_group.id, "corps_id": corps.id,
            "other_corps_id": other_corps.id, "brigade_id": brigade.id}


def _category_create(client, headers, user, ids):
    return client.post(V1.path(CategoryRoutes.CREATE), headers=headers(user), json={"name": "New category", "alias": "nc"})


def _category_update(client, headers, user, ids):
    return client.put(V1.path(CategoryRoutes.CATEGORY_BY_ID, category_id=ids["category_id"]), headers=headers(user),
                      json={"name": "Renamed category"})


def _signal_type_create(client, headers, user, ids):
    return client.post(V1.path(SignalTypeRoutes.CREATE), headers=headers(user),
                       json={"code": "NEWST", "name": "New signal type", "category_id": ids["category_id"]})


def _signal_type_update(client, headers, user, ids):
    return client.put(V1.path(SignalTypeRoutes.SIGNAL_TYPE_BY_ID, signal_type_id=ids["signal_type_id"]),
                      headers=headers(user), json={"name": "Renamed signal type"})


def _tag_create(client, headers, user, ids):
    return client.post(V1.path(TagRoutes.CREATE), headers=headers(user), json={"name": "newtag"})


def _tag_update(client, headers, user, ids):
    return client.put(V1.path(TagRoutes.TAG_BY_ID, tag_id=ids["tag_id"]), headers=headers(user),
                      json={"name": "renamedtag"})


def _format_payload():
    return {"media_type": "image", "content_type": "image/avif", "label": "AVIF", "extensions": [".avif"],
            "aliases": [], "max_file_size_bytes": 1024}


def _format_create(client, headers, user, ids):
    return client.post(V1.path(MediaFormatRoutes.CREATE), headers=headers(user), json=_format_payload())


def _format_update(client, headers, user, ids):
    return client.put(V1.path(MediaFormatRoutes.FORMAT_BY_ID, format_id=ids["format_id"]), headers=headers(user),
                      json={"label": "Renamed format"})


def _post_create(client, headers, user, ids):
    return client.post(V1.path(PostRoutes.ROOT), headers=headers(user), json={"name": "New post", "code": "NEWPOST"})


def _post_update(client, headers, user, ids):
    return client.put(V1.path(PostRoutes.POST_BY_ID, post_id=ids["post_id"]), headers=headers(user),
                      json={"name": "Renamed post"})


def _role_create(client, headers, user, ids):
    return client.post(V1.path(RoleRoutes.ROOT), headers=headers(user),
                       json={"name": "new_role", "display_name": "New role", "permission_ids": [ids["view_permission_id"]]})


def _role_update(client, headers, user, ids):
    return client.patch(V1.path(RoleRoutes.ROLE_BY_ID, role_id=ids["role_id"]), headers=headers(user),
                        json={"name": "renamed_role", "permission_ids": [ids["create_permission_id"]]})


def _role_clone(client, headers, user, ids):
    return client.post(V1.path(RoleRoutes.ROLE_CLONE, role_id=ids["role_id"]), headers=headers(user),
                       json={"name": "cloned_role", "display_name": "Cloned role"})


def _permission_add(client, headers, user, ids):
    return client.put(V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=ids["empty_role_id"],
                              permission_id=ids["view_permission_id"]), headers=headers(user))


def _permission_remove(client, headers, user, ids):
    return client.delete(V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=ids["role_id"],
                                 permission_id=ids["view_permission_id"]), headers=headers(user))


def _user_create(client, headers, user, ids):
    return client.post(V1.path(UserRoutes.ROOT), headers=headers(user),
                       data={"username": "newuser", "username_alias": "New", "user_type": "viewer"})


def _group_create(client, headers, user, ids):
    return client.post(V1.path(ScopeGroupRoutes.ROOT), headers=headers(user),
                       json={"name": "New group", "parent_id": ids["group_id"]})


def _group_update(client, headers, user, ids):
    return client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=ids["group_id"]), headers=headers(user),
                        json={"name": "Renamed group"})


def _group_move(client, headers, user, ids):
    return client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=ids["group_id"]), headers=headers(user),
                        json={"parent_id": ids["other_group_id"]})


def _corps_create(client, headers, user, ids):
    return client.post(V1.path(CorpsRoutes.ROOT), headers=headers(user), json={"name": "New corps"})


def _corps_update(client, headers, user, ids):
    return client.patch(V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=ids["corps_id"]), headers=headers(user),
                        json={"name": "Renamed corps"})


def _brigade_create(client, headers, user, ids):
    return client.post(V1.path(BrigadeRoutes.ROOT), headers=headers(user),
                       json={"name": "New brigade", "corps_id": ids["corps_id"]})


def _brigade_update(client, headers, user, ids):
    return client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=ids["brigade_id"]), headers=headers(user),
                        json={"name": "Renamed brigade"})


def _brigade_move(client, headers, user, ids):
    return client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=ids["brigade_id"]), headers=headers(user),
                        json={"corps_id": ids["other_corps_id"]})


GROUPS = "api.shared.helpers.scope_group_helper"

WRITES = {
    "category_create": Case("categories", Category, "category", "create", _category_create),
    "category_update": Case("categories", Category, "category", "update", _category_update, "category_id"),
    "signal_type_create": Case("signal_types", SignalType, "signal_type", "create", _signal_type_create),
    "signal_type_update": Case("signal_types", SignalType, "signal_type", "update", _signal_type_update,
                               "signal_type_id"),
    "tag_create": Case("tags", Tag, "tag", "create", _tag_create),
    "tag_update": Case("tags", Tag, "tag", "update", _tag_update, "tag_id"),
    "media_format_create": Case("media_formats", MediaFormat, "media_format", "create", _format_create),
    "media_format_update": Case("media_formats", MediaFormat, "media_format", "update", _format_update, "format_id"),
    "post_create": Case("posts", Post, "post", "create", _post_create),
    "post_update": Case("posts", Post, "post", "update", _post_update, "post_id"),
    "role_create": Case("roles", Role, "role", "create", _role_create, status=201),
    "role_update": Case("roles", Role, "role", "update", _role_update, "role_id"),
    "role_clone": Case("roles", Role, "role", "create", _role_clone, status=201),
    "role_permission_add": Case("roles", Role, "role", "update", _permission_add, "empty_role_id",
                                row_id=lambda ids, body: ids["empty_role_id"]),
    "role_permission_remove": Case("roles", Role, "role", "update", _permission_remove, "role_id",
                                   row_id=lambda ids, body: ids["role_id"]),
    "user_create": Case("users", User, "user", "create", _user_create),
    "scope_group_create": Case(GROUPS, ScopeGroup, "scope_group", "create", _group_create),
    "scope_group_update": Case(GROUPS, ScopeGroup, "scope_group", "update", _group_update, "group_id"),
    "scope_group_move": Case(GROUPS, ScopeGroup, "scope_group", "update", _group_move, "group_id"),
    "corps_create": Case(GROUPS, ScopeGroup, "scope_group", "create", _corps_create, status=201),
    "corps_update": Case(GROUPS, ScopeGroup, "scope_group", "update", _corps_update, "corps_id"),
    "brigade_create": Case(GROUPS, ScopeGroup, "scope_group", "create", _brigade_create, status=201),
    "brigade_update": Case(GROUPS, ScopeGroup, "scope_group", "update", _brigade_update, "brigade_id"),
    "brigade_move": Case(GROUPS, ScopeGroup, "scope_group", "update", _brigade_move, "brigade_id"),
}


async def _stored(session_maker, case: Case, ids):
    """What the route may change: the row of an update (every column), the number of rows of a create"""
    async with session_maker() as fresh:
        if case.target is None:
            return (await fresh.exec(select(func.count()).select_from(case.model))).one()
        return (await fresh.get(case.model, ids[case.target])).model_dump()


async def _audit_count(session_maker, resource_type):
    async with session_maker() as fresh:
        return (await fresh.exec(select(func.count()).select_from(AuditLog).where(
            AuditLog.resource_type == resource_type))).one()


@pytest.mark.parametrize("name", list(WRITES))
async def test_a_failed_audit_row_stores_no_change(client, db, headers, session_maker, monkeypatch, name):
    import importlib
    case = WRITES[name]
    module = importlib.import_module(
        f"app.backend.{case.module}" if "." in case.module else f"app.backend.api.v1.{case.module}")
    ids = await _scene(db)
    before = await _stored(session_maker, case, ids)
    audits_before = await _audit_count(session_maker, case.resource_type)

    monkeypatch.setattr(module, "create_audit_log", _audit_fails)
    response = await case.request(client, headers, ids["user"], ids)
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text

    assert await _stored(session_maker, case, ids) == before
    assert await _audit_count(session_maker, case.resource_type) == audits_before


@pytest.mark.parametrize("name", list(WRITES))
async def test_the_change_and_one_audit_row_are_stored(client, db, headers, session_maker, name):
    case = WRITES[name]
    ids = await _scene(db)
    before = await _stored(session_maker, case, ids)

    response = await case.request(client, headers, ids["user"], ids)
    assert response.status_code == case.status, response.text
    body = response.json()
    audited = case.row_id(ids, body) if case.row_id else body["id"]

    after = await _stored(session_maker, case, ids)
    assert after != before
    async with session_maker() as fresh:
        (audit,) = (await fresh.exec(select(AuditLog).where(
            AuditLog.resource_type == case.resource_type, AuditLog.action == case.action,
            AuditLog.resource_id == audited))).all()
    assert audit.user_id == ids["user"].id
    if case.row_id is None:
        assert audit.new_values["id"] == audited
    if case.target is None:
        assert audit.old_values is None
    else:
        assert audit.resource_id == ids[case.target] and audit.old_values != audit.new_values


async def test_a_failed_audit_row_stores_no_user_and_no_certificate(client, db, headers, session_maker, monkeypatch):
    """A user created with an uploaded certificate: the user, its assignment and its certificate row go with the audit"""
    from app.backend.api.v1 import users

    ids = await _scene(db)

    async def counts():
        async with session_maker() as fresh:
            return [(await fresh.exec(select(func.count()).select_from(model))).one() for model in (User, UserCertificate)]

    before = await counts()
    monkeypatch.setattr(users, "create_audit_log", _audit_fails)
    response = await client.post(
        V1.path(UserRoutes.ROOT), headers=headers(ids["user"]),
        data={"username": "certuser", "username_alias": "Cert", "user_type": "viewer"},
        files={"certificate": ("cert.pem", make_certificate("cn-atomic"), "application/x-pem-file")})
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    assert await counts() == before
