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
from app.backend.enums.routes import CategoryRoutes, MediaFormatRoutes, SignalTypeRoutes, TagRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.media_format import MediaFormat
from app.backend.models.observation import Category, SignalType, Tag
from tests.helpers import make_admin, make_dictionary, make_post


async def _audit_fails(*args, **kwargs):
    raise OperationalError("INSERT INTO audit_logs ...", {}, Exception("(1020, 'Record has changed since last read')"))


@dataclass
class Case:
    """module: api.v1 module whose create_audit_log is replaced; model/resource_type/action: what is stored;
    request(client, headers, user, ids) -> response; ids: the rows of the scene by name;
    row_id(ids, response) -> id of the audited row"""
    module: str
    model: Any
    resource_type: str
    action: str
    request: Callable[..., Awaitable[Any]]
    # None: a create (the row is counted); name of ids: the row that is updated
    target: Optional[str] = None


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
    return {"user": user, **ids, "other_category_id": other["category_id"], "format_id": fmt.id}


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
    module = importlib.import_module(f"app.backend.api.v1.{case.module}")
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
    assert response.status_code == 200, response.text
    body = response.json()

    after = await _stored(session_maker, case, ids)
    assert after != before
    async with session_maker() as fresh:
        (audit,) = (await fresh.exec(select(AuditLog).where(
            AuditLog.resource_type == case.resource_type, AuditLog.action == case.action,
            AuditLog.resource_id == body["id"]))).all()
    assert audit.user_id == ids["user"].id
    assert audit.new_values["id"] == body["id"]
    if case.target is None:
        assert audit.old_values is None
    else:
        assert audit.resource_id == ids[case.target] and audit.old_values != audit.new_values
