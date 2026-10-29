"""A write route commits its change and its audit row in ONE transaction.

Each route used to commit the change and write the audit row in a second transaction: a failure there (on MariaDB the
audit row's foreign key check of the caller's users row answers 1020 after the refresh opened a read view; a lock
wait timeout; a lost connection) answered 500 for a stored change and lost the audit row.

Add a route to WRITES (one Case): a route module name, how to build the scene, the request and what to read back.
Every Case runs two tests: a failing create_audit_log stores nothing, and the happy path stores one audit row."""
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import pytest
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import object_session
from sqlmodel import select

from app.backend.api.shared.helpers import upload_helper
from app.backend.api.v1 import V1, media_uploads, search
from app.backend.config import settings
from app.backend.enums.routes import (
    BrigadeRoutes, CategoryRoutes, CorpsRoutes, GuideRoutes, MediaFormatRoutes, MediaRoutes, PostRoutes, RoleRoutes,
    ScopeGroupRoutes, SearchRoutes, SignalTypeRoutes, TagRoutes, UserRoutes,
)
from app.backend.models.audit import AuditLog
from app.backend.models.media import MediaFile
from app.backend.models.media_format import MediaFormat
from app.backend.models.upload_session import UploadSession
from app.backend.services.media.manager import reset_media_manager
from app.backend.models.observation import Category, SignalType, Tag
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user_certificate import UserCertificate
from app.backend.models.user import User
from app.backend.models.user_guide import UserGuide
from tests.helpers import make_admin, make_dictionary, make_group, make_observation, make_post, make_role, make_user
from tests.test_poster_jobs import MP4_BYTES
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
        Permission.name.in_(["tag.view", "post.view"])))).all()}
    return {"user": user, **ids, "other_category_id": other["category_id"], "format_id": fmt.id,
            "post_id": (await make_post(db)).id, "role_id": role.id, "empty_role_id": empty_role.id,
            "view_permission_id": permissions["tag.view"], "create_permission_id": permissions["post.view"],
            "group_id": group.id, "other_group_id": other_group.id, "corps_id": corps.id,
            "other_corps_id": other_corps.id, "brigade_id": brigade.id, "guide_id": await _guide(db, user)}


async def _guide(db, user) -> int:
    guide = UserGuide(title="A guide", created_by=user.id, updated_by=user.id)
    db.add(guide)
    await db.commit()
    await db.refresh(guide)
    return guide.id


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
                       json={"name": "new_role", "display_name": "New role", "kind": "tree",
                             "permission_ids": [ids["view_permission_id"]]})


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


def _guide_create(client, headers, user, ids):
    return client.post(V1.path(GuideRoutes.ROOT), headers=headers(user), json={"title": "New guide"})


def _guide_update(client, headers, user, ids):
    return client.patch(V1.path(GuideRoutes.GUIDE_BY_ID, guide_id=ids["guide_id"]), headers=headers(user),
                        json={"title": "Renamed guide", "audience": {"kind": "post", "id": ids["post_id"]}})


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
    "role_clone": Case("roles", Role, "role", "clone", _role_clone, status=201),
    "role_permission_add": Case("roles", Role, "role", "permission_add", _permission_add, "empty_role_id",
                                row_id=lambda ids, body: ids["empty_role_id"]),
    "role_permission_remove": Case("roles", Role, "role", "permission_remove", _permission_remove, "role_id",
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
    "guide_create": Case("guides", UserGuide, "user_guide", "create", _guide_create, status=201),
    "guide_update": Case("guides", UserGuide, "user_guide", "update", _guide_update, "guide_id"),
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


# ---------------------------------------------------------------------------------------------------- media writes
# These routes do not fit a Case: an upload session is audited with resource_id None, and an upload stores a file,
# which no transaction can take back.

@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "_tmp"))
    reset_media_manager()
    yield tmp_path
    reset_media_manager()


async def _media_scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "media.create"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return user, observation


def _open_session(client, headers, user, observation):
    return client.post(V1.path(MediaRoutes.UPLOADS), headers=headers(user), json={
        "observation_id": observation.id, "media_type": "image", "original_filename": "photo.png",
        "expected_size": 100, "content_type": "image/png"})


async def _open_upload_session(client, headers, user, observation, storage):
    """An open session with a part file of 10 bytes"""
    created = await _open_session(client, headers, user, observation)
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    part = storage / "_tmp" / "uploads" / f"{upload_id}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 10)
    return upload_id, part


async def _audit_rows(session_maker, resource_type, action):
    async with session_maker() as fresh:
        return (await fresh.exec(select(AuditLog).where(
            AuditLog.resource_type == resource_type, AuditLog.action == action))).all()


async def test_a_failed_audit_row_opens_no_upload_session(client, db, headers, session_maker, monkeypatch, storage):
    user, observation = await _media_scene(db)
    monkeypatch.setattr(media_uploads, "create_audit_log", _audit_fails)
    response = await _open_session(client, headers, user, observation)
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    async with session_maker() as fresh:
        assert (await fresh.exec(select(func.count()).select_from(UploadSession))).one() == 0
    assert await _audit_rows(session_maker, "upload_session", "create") == []


async def test_an_upload_session_and_its_audit_row_are_stored(client, db, headers, session_maker, storage):
    user, observation = await _media_scene(db)
    upload_id, _ = await _open_upload_session(client, headers, user, observation, storage)
    (audit,) = await _audit_rows(session_maker, "upload_session", "create")
    assert audit.new_values["upload_id"] == upload_id and audit.user_id == user.id


async def test_a_failed_audit_row_aborts_no_upload_session(client, db, headers, session_maker, monkeypatch, storage):
    """The abort and its audit row commit together; the part file is removed only after that commit"""
    user, observation = await _media_scene(db)
    upload_id, part = await _open_upload_session(client, headers, user, observation, storage)
    monkeypatch.setattr(media_uploads, "create_audit_log", _audit_fails)
    response = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(user))
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    async with session_maker() as fresh:
        row = await fresh.get(UploadSession, upload_id)
    assert (row.status, row.error, row.writer_token) == ("open", None, None)
    assert part.read_bytes() == b"x" * 10, "the received bytes stay with the open session"
    assert await _audit_rows(session_maker, "upload_session", "abort") == []


async def test_an_abort_and_its_audit_row_are_stored(client, db, headers, session_maker, storage):
    user, observation = await _media_scene(db)
    upload_id, part = await _open_upload_session(client, headers, user, observation, storage)
    response = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(user))
    assert response.status_code == 200 and response.json()["status"] == "aborted", response.text
    assert not part.exists()
    (audit,) = await _audit_rows(session_maker, "upload_session", "abort")
    assert audit.params["upload_id"] == upload_id and audit.details is None


def _upload_video(client, headers, user, observation):
    return client.post(
        V1.path(MediaRoutes.UPLOAD), data={"observation_id": observation.id, "media_type": "video"},
        files={"file": ("clip.mp4", MP4_BYTES, "video/mp4")}, headers=headers(user))


def _probe_outside_a_transaction(monkeypatch, seen):
    """ffprobe stand-in: records whether the request session held a transaction (a read view) while it ran"""
    async def probe(media_file):
        seen.append(object_session(media_file).in_transaction())
        return {"duration_seconds": 3.0}

    monkeypatch.setattr(upload_helper, "_probe_headers", probe)


async def test_a_failed_audit_row_keeps_the_poster_hold_of_an_upload(client, db, headers, session_maker, monkeypatch, storage):
    """The file is stored (a move cannot be rolled back) and its row stays, but the poster hold release, the probed
    metadata and the audit row commit together: none of them is stored. The hold runs out after UPLOAD_HOLD and the
    worker takes the poster then."""
    user, observation = await _media_scene(db)
    seen = []
    _probe_outside_a_transaction(monkeypatch, seen)
    monkeypatch.setattr(upload_helper, "create_audit_log", _audit_fails)
    response = await _upload_video(client, headers, user, observation)
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    async with session_maker() as fresh:
        (row,) = (await fresh.exec(select(MediaFile).where(MediaFile.observation_id == observation.id))).all()
    assert row.resolve_path(storage).exists()
    assert (row.poster_status, row.poster_attempts) == ("pending", 0)
    assert row.poster_locked_until is not None, "still held"
    assert row.duration_seconds is None
    assert await _audit_rows(session_maker, "media", "create") == []
    assert seen == [False]


async def test_an_upload_releases_its_poster_hold_with_its_audit_row(client, db, headers, session_maker, monkeypatch, storage):
    user, observation = await _media_scene(db)
    seen = []
    _probe_outside_a_transaction(monkeypatch, seen)
    response = await _upload_video(client, headers, user, observation)
    assert response.status_code == 200, response.text
    assert seen == [False], "no read view of the request lives across the probe"
    async with session_maker() as fresh:
        row = await fresh.get(MediaFile, response.json()["media_file_id"])
    assert (row.poster_status, row.poster_locked_until, row.duration_seconds) == ("pending", None, 3.0)
    (audit,) = await _audit_rows(session_maker, "media", "create")
    assert audit.resource_id == row.id
    assert audit.new_values["poster_locked_until"] is None and audit.new_values["duration_seconds"] == 3.0


async def test_search_writes_its_audit_row_in_a_new_transaction(client, db, headers, monkeypatch):
    """The audit row of a search is not written at the end of the read view the request opened (L3)"""
    user = await make_admin(db)
    real = search.create_audit_log
    seen = []

    async def audit(**kwargs):
        seen.append(kwargs["session"].in_transaction())
        return await real(**kwargs)

    monkeypatch.setattr(search, "create_audit_log", audit)
    response = await client.get(V1.path(SearchRoutes.TAGS), headers=headers(user))
    assert response.status_code == 200, response.text
    assert seen == [False]


async def test_a_nginx_upload_releases_its_poster_hold(client, db, headers, session_maker, monkeypatch, storage, tmp_path):
    user, observation = await _media_scene(db)
    _probe_outside_a_transaction(monkeypatch, [])
    temp_dir = tmp_path / "nginx_uploads"
    temp_dir.mkdir()
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(temp_dir))
    temp_file = temp_dir / "0000000001"
    temp_file.write_bytes(MP4_BYTES)
    response = await client.post(
        V1.path(MediaRoutes.NGINX_UPLOAD), params={"observation_id": observation.id, "media_type": "video"},
        headers={"X-File-Name": str(temp_file), "X-Original-Filename": "clip.mp4", "Content-Type": "video/mp4",
                 **headers(user)},
        content=b"",
    )
    assert response.status_code in (200, 201), response.text
    async with session_maker() as fresh:
        row = await fresh.get(MediaFile, response.json()["media_file_id"])
    assert (row.poster_status, row.poster_locked_until) == ("pending", None)


async def test_a_resumable_complete_releases_its_poster_hold(client, db, headers, session_maker, monkeypatch, storage):
    user, observation = await _media_scene(db)
    _probe_outside_a_transaction(monkeypatch, [])
    created = await client.post(V1.path(MediaRoutes.UPLOADS), headers=headers(user), json={
        "observation_id": observation.id, "media_type": "video", "original_filename": "clip.mp4",
        "expected_size": len(MP4_BYTES), "content_type": "video/mp4"})
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    sent = await client.patch(
        V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id), content=MP4_BYTES,
        headers={"Content-Type": "application/offset+octet-stream", "Upload-Offset": "0", **headers(user)})
    assert sent.status_code == 204, sent.text
    done = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id), headers=headers(user))
    assert done.status_code == 200, done.text
    async with session_maker() as fresh:
        row = await fresh.get(MediaFile, done.json()["media_file_id"])
    assert (row.poster_status, row.poster_locked_until) == ("pending", None)


# --- static guard: no audit row after a commit --------------------------------------------------------------------

# Wrappers of create_audit_log: observations._audit_frequency, search.log_search, user_certificates._audit (builds the
# WriteAudit callback), scope_group_helper.audit_group
AUDIT_WRITERS = {"create_audit_log", "audit_group", "_audit_frequency", "log_search", "_audit"}
# A call passing one of these keywords hands a callable that writes the audit row to a service (posts._store_post,
# media_formats._store_with_audit, uploads.abort_session, certificates.*): the audit row is written by that call.
AUDIT_CALLBACK_KEYWORDS = {"audit", "record", "write_audit"}
# Scanned: api/ (the routes), services/ and auth/ (a service or AuthService could commit and then audit just as well).
GUARDED_DIRS = ("api", "services", "auth")
# "relative/path.py:function" -> why a commit before the audit row is by design
ALLOWED_AUDIT_AFTER_COMMIT: Dict[str, str] = {
    "services/unit_history.py:fill_unit_history": "CLI command, not a route: the fill commits per chunk of rows on "
                                                  "purpose (it can be huge) and writes one summary audit row at the end",
}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_audit_write(call: ast.Call) -> bool:
    return _call_name(call) in AUDIT_WRITERS or any(k.arg in AUDIT_CALLBACK_KEYWORDS for k in call.keywords)


def _own_calls(function: ast.AST):
    """(call, exits) of one function in source order, without nested functions, lambdas and classes.

    exits: the call sits in a statement list that is not the function body and in which a return / raise follows
    (an early exit branch: an if / else branch, an except handler, a with-block inside one). A commit there is
    followed by the exit, never by the audit row of the main path; but not when an audit write follows it in that
    same branch (commit, audit, return). break / continue do not leave the function."""
    found = []

    def expressions(node, exits):
        if isinstance(node, _SCOPES):
            return
        if isinstance(node, ast.Call):
            found.append((node, exits))
            if exits is not None:
                exits.append(node)
        for child in ast.iter_child_nodes(node):
            expressions(child, exits)

    def block(stmts, exits):
        leaves = exits
        if leaves is None and any(isinstance(st, (ast.Return, ast.Raise)) for st in stmts):
            leaves = []                       # collects every call of this exit branch
        for st in stmts:
            statement(st, leaves)

    def statement(st, exits):
        if isinstance(st, _SCOPES):
            return
        inherited = exits if not isinstance(st, (ast.For, ast.AsyncFor, ast.While)) else None
        for _, value in ast.iter_fields(st):
            items = value if isinstance(value, list) else [value]
            if isinstance(value, list) and value and all(isinstance(i, (ast.stmt, ast.ExceptHandler, ast.match_case)) for i in items):
                if all(isinstance(i, ast.stmt) for i in items):
                    block(items, inherited)
                else:
                    for item in items:
                        block(item.body, inherited)
            else:
                for item in items:
                    if isinstance(item, ast.AST) and not isinstance(item, ast.stmt):
                        expressions(item, exits)

    for st in function.body:
        statement(st, None)
    ordered = sorted(found, key=lambda item: (item[0].lineno, item[0].col_offset))
    return [(call, branch is not None and not any(
        _is_audit_write(other) and (other.lineno, other.col_offset) > (call.lineno, call.col_offset)
        for other in branch)) for call, branch in ordered]


def audit_after_commit(source: str, filename: str = "<source>"):
    """(function, line) of every audit-writing call that follows a .commit() call in the same function body.

    Static: a helper that commits inside (a service called before the audit row) is not seen here; the runtime Cases
    of WRITES cover those, because a failing create_audit_log must leave no change behind."""
    problems = []
    for function in ast.walk(ast.parse(source, filename)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        committed_at = None
        for call, leaves in _own_calls(function):
            if _call_name(call) == "commit" and committed_at is None and not leaves:
                committed_at = call.lineno
            elif _is_audit_write(call) and committed_at is not None:
                problems.append((function.name, call.lineno))
    return problems


def test_no_audit_after_commit():
    root = Path(__file__).resolve().parent.parent / "app" / "backend"
    found = []
    for directory in GUARDED_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            for function, line in audit_after_commit(path.read_text(encoding="utf-8"), relative):
                if f"{relative}:{function}" not in ALLOWED_AUDIT_AFTER_COMMIT:
                    found.append(f"{relative}:{function}:{line}")
    assert not found, "audit row written after a commit (one transaction, or add a reasoned allowlist entry): " + ", ".join(found)


def test_the_audit_guard_bites():
    broken = (
        "async def route(session):\n"
        "    await session.commit()\n"
        "    await create_audit_log(session=session)\n"
    )
    assert audit_after_commit(broken) == [("route", 3)]
    grouped = "async def route(session):\n    await session.commit()\n    await audit_group(session)\n"
    assert audit_after_commit(grouped) == [("route", 3)]
    good = (
        "async def route(session):\n"
        "    await create_audit_log(session=session)\n"
        "    await session.commit()\n"
    )
    assert audit_after_commit(good) == []
    nested = (
        "async def route(session):\n"
        "    async def inner():\n"
        "        await create_audit_log(session=session)\n"
        "    await session.commit()\n"
    )
    assert audit_after_commit(nested) == []
    early_exit = (
        "async def route(session, done):\n"
        "    if done:\n"
        "        await session.commit()\n"
        "        return False\n"
        "    await create_audit_log(session=session)\n"
        "    await session.commit()\n"
    )
    assert audit_after_commit(early_exit) == []
    branch_then_audit = (
        "async def route(session, done):\n"
        "    if done:\n"
        "        await session.commit()\n"
        "    await create_audit_log(session=session)\n"
    )
    assert audit_after_commit(branch_then_audit) == [("route", 4)]


def test_the_audit_guard_bites_where_the_function_returns():
    samples = {
        "ends_in_return": (
            "async def route(s):\n"
            "    await s.commit()\n"
            "    await create_audit_log(session=s)\n"
            "    return 1\n"
        , [('route', 3)]),
        "wrapper": (
            "async def route(s):\n"
            "    await s.commit()\n"
            "    await _audit_frequency(s)\n"
            "    return 1\n"
        , [('route', 3)]),
        "callback": (
            "async def route(s):\n"
            "    await s.commit()\n"
            "    await store(s, audit=lambda: 1)\n"
            "    return 1\n"
        , [('route', 3)]),
        "loop_break": (
            "async def route(s, rows):\n"
            "    for r in rows:\n"
            "        if r:\n"
            "            await s.commit()\n"
            "            break\n"
            "    await create_audit_log(session=s)\n"
        , [('route', 6)]),
        "try_except_return": (
            "async def route(s):\n"
            "    try:\n"
            "        await s.commit()\n"
            "    except OSError:\n"
            "        return 1\n"
            "    await create_audit_log(session=s)\n"
        , [('route', 6)]),
        "auth_style": (
            "async def route(s, done):\n"
            "    if done:\n"
            "        await s.commit()\n"
            "        return False\n"
            "    await create_audit_log(session=s)\n"
            "    await s.commit()\n"
            "    return True\n"
        , []),
        "else_branch_exit": (
            "async def route(s, done):\n"
            "    if done:\n"
            "        pass\n"
            "    else:\n"
            "        await s.commit()\n"
            "        raise ValueError()\n"
            "    await create_audit_log(session=s)\n"
        , []),
        "audit_first": (
            "async def route(s):\n"
            "    await create_audit_log(session=s)\n"
            "    await s.commit()\n"
            "    return 1\n"
        , []),
        "branch_commit_then_audit": (
            "async def route(s, x):\n"
            "    if x:\n"
            "        await s.commit()\n"
            "        await create_audit_log(session=s)\n"
            "        return 1\n"
            "    return 2\n"
        , [("route", 4)]),
        "with_body_commit_then_audit": (
            "async def route(s):\n"
            "    async with s.begin():\n"
            "        await s.commit()\n"
            "        await create_audit_log(session=s)\n"
            "        return 1\n"
        , [("route", 4)]),
        "try_body_commit_then_audit": (
            "async def route(s):\n"
            "    try:\n"
            "        await s.commit()\n"
            "        await create_audit_log(session=s)\n"
            "        return 1\n"
            "    finally:\n"
            "        pass\n"
        , [("route", 4)]),
        "branch_audit_then_commit": (
            "async def route(s, x):\n"
            "    if x:\n"
            "        await create_audit_log(session=s)\n"
            "        await s.commit()\n"
            "        return 1\n"
        , []),
    }
    for name, (source, expected) in samples.items():
        assert audit_after_commit(source) == expected, name
