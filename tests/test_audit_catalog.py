"""The audit event catalog (enums/audit.py): codes, categories, params"""
import re
from typing import get_args

from app.backend.enums.audit import (
    SERVED_ONLY, SWITCHABLE, AuditCategory, AuditEvent, EventSpec, audit_event, event,
)
from app.backend.errors.coded import RESERVED_MEMBERS


def test_codes_are_unique_and_resource_dot_action():
    codes = [member.code for member in AuditEvent]
    assert len(codes) == len(set(codes))
    for code in codes:
        assert re.fullmatch(r"^[a-z_]+\.[a-z_]+$", code), code


def test_every_param_name_has_three_chars_and_is_not_reserved():
    for member in AuditEvent:
        for name in member.params:
            assert len(name) >= 3, f"{member.code}.{name}"
            assert name not in RESERVED_MEMBERS, f"{member.code}.{name} is reserved"


def test_audit_category_is_not_switchable():
    assert AuditCategory.AUDIT not in SWITCHABLE
    assert SWITCHABLE == frozenset(AuditCategory) - {AuditCategory.AUDIT}


def test_every_category_has_an_event():
    assert {member.category for member in AuditEvent} == set(AuditCategory)


def test_lookup_and_spec():
    assert audit_event("user.login") is AuditEvent.USER_LOGIN
    assert audit_event("nope.nope") is None
    assert isinstance(AuditEvent.USER_LOGIN.spec, EventSpec)
    assert AuditEvent.USER_LOGIN.category is AuditCategory.AUTHENTICATION
    assert event(AuditCategory.DATA, "T", a=int).params == {"a": int}


def test_optional_params_are_typed_optional():
    params = AuditEvent.SESSION_END.params
    assert type(None) in get_args(params["session_count"])
    assert params["username"] is str


def test_titles_are_fixed_text():
    for member in AuditEvent:
        assert member.title and "{" not in member.title, member.code


def test_search_and_dictionary_events():
    for entity in ("observations", "users", "posts", "roles", "categories", "signal_types", "tags", "media",
                   "media_formats"):
        assert audit_event(f"search.{entity}").category is AuditCategory.READS
    for resource in ("category", "tag", "signal_type", "media_format"):
        for action in ("create", "update", "archive", "restore"):
            assert audit_event(f"{resource}.{action}").category is AuditCategory.DICTIONARIES


# --- create_audit_log(event=...): category, scope, request id, params ----------------------------------------------

import pytest
from sqlalchemy import text as sa_text

from app.backend.auth.scope import Node
from app.backend.errors.coded import is_optional
from app.backend.migrations.support.audit_events_v1 import DETAILS_PARAMS, LEGACY_EVENTS, LEGACY_MIGRATE, LEGACY_UNKNOWN
from app.backend.models.audit import AuditLog
from app.backend.services import audit as audit_service
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import request_id_context

REQUEST_ID = "0123456789abcdef0123456789abcdef"


async def _stored(session, row: AuditLog) -> AuditLog:
    await session.commit()
    session.expunge_all()
    return await session.get(AuditLog, row.id)


async def test_row_gets_category_scope_request_id(db):
    token = request_id_context.set(REQUEST_ID)
    try:
        row = await create_audit_log(db, AuditEvent.POST_UPDATE, scope=Node.post(7), resource_id=7, user_id=3,
                                     params={"post_name": "North", "post_code": "N-1"})
        group_row = await create_audit_log(db, AuditEvent.SCOPE_GROUP_ARCHIVE, scope=Node.group(4), resource_id=4,
                                           params={"group_name": "East", "group_kind": "brigade"})
    finally:
        request_id_context.reset(token)
    system_row = await create_audit_log(db, AuditEvent.SEARCH_TAGS, params={"result_count": 2})

    stored = await _stored(db, row)
    assert (stored.event, stored.category, stored.resource_type, stored.action) == (
        "post.update", "structure", "post", "update")
    assert (stored.scope_key, stored.scope_post_id, stored.scope_group_id) == ("p:7", 7, None)
    assert stored.request_id == REQUEST_ID
    assert stored.params == {"post_name": "North", "post_code": "N-1"}
    assert (stored.resource_id, stored.user_id, stored.details) == (7, 3, None)

    group = await db.get(AuditLog, group_row.id)
    assert (group.scope_key, group.scope_post_id, group.scope_group_id) == ("g:4", None, 4)
    assert group.category == "structure"

    system = await db.get(AuditLog, system_row.id)
    assert (system.scope_key, system.scope_post_id, system.scope_group_id, system.request_id) == (None,) * 4
    assert (system.event, system.category, system.resource_type, system.action) == (
        "search.tags", "reads", "tag", "search")


async def test_search_events_keep_action_search_with_the_singular_entity(db):
    row = await create_audit_log(db, AuditEvent.SEARCH_OBSERVATIONS, params={"result_count": 3})
    stored = await _stored(db, row)
    assert (stored.event, stored.category, stored.action, stored.resource_type) == (
        "search.observations", "reads", "search", "observation")
    singular = {"observations": "observation", "users": "user", "posts": "post", "roles": "role",
                "categories": "category", "signal_types": "signal_type", "tags": "tag", "media": "media",
                "media_formats": "media_format"}
    for member in AuditEvent:
        resource_type, action = audit_service.writer.resource_and_action(member)
        if member.code.startswith("search."):
            assert (resource_type, action) == (singular[member.code.split(".", 1)[1]], "search"), member.code
        else:
            assert f"{resource_type}.{action}" == member.code


async def test_a_row_without_params_stores_sql_null(db):
    row = await create_audit_log(db, AuditEvent.SEARCH_TAGS, params={"result_count": 1})
    empty = await create_audit_log(db, AuditEvent.MEDIA_ARCHIVE)
    await db.commit()
    found = (await db.execute(sa_text("select id from audit_logs where params is null"))).all()
    assert {row_id for (row_id,) in found} == {empty.id}
    assert row.id not in {row_id for (row_id,) in found}


async def test_wrong_params_raise_type_error(db):
    with pytest.raises(TypeError, match="unknown"):
        await create_audit_log(db, AuditEvent.TAG_CREATE, params={"tag_name": "x", "colour": "red"})
    with pytest.raises(TypeError, match="missing"):
        await create_audit_log(db, AuditEvent.POST_CREATE, params={"post_name": "North"})
    with pytest.raises(TypeError, match="missing"):
        await create_audit_log(db, AuditEvent.TAG_CREATE)
    with pytest.raises(TypeError):
        await create_audit_log(db, "tag.create", params={"tag_name": "x"})
    # Optional params may be left out; values go through serialize_for_audit; workspace is accepted and ignored
    row = await create_audit_log(db, AuditEvent.SESSION_END, workspace=Node.group(2),
                                 params={"username": "ivan", "cause": AuditCategory.ACCESS})
    stored = await _stored(db, row)
    assert stored.params == {"username": "ivan", "cause": "access"}


async def test_none_params_are_absent_or_refused(db):
    """An Optional param passed as None is left out (absent, never null); None in a required param or under an
    unknown name is a TypeError"""
    row = await create_audit_log(db, AuditEvent.USER_CERTIFICATE_REVOKE, params={
        "username": "ivan", "certificate_label": "", "certificate_serial": None, "reason": "superseded"})
    stored = await _stored(db, row)
    assert stored.params == {"username": "ivan", "certificate_label": "", "reason": "superseded"}
    with pytest.raises(TypeError, match="required params"):
        await create_audit_log(db, AuditEvent.TAG_CREATE, params={"tag_name": None})
    with pytest.raises(TypeError, match="unknown"):
        await create_audit_log(db, AuditEvent.TAG_CREATE, params={"tag_name": "x", "colour": None})


@pytest.mark.parametrize("served_only", sorted(SERVED_ONLY, key=lambda e: e.code), ids=lambda e: e.code)
async def test_served_only_events_are_never_written(db, served_only):
    """legacy.unknown and legacy.migrate are served by the read API, so their served params (stored_event, details)
    can never be written either"""
    with pytest.raises(TypeError, match="never written"):
        await create_audit_log(db, served_only, params={"action": "x", "resource_type": "y"})
    assert SERVED_ONLY == {AuditEvent.LEGACY_UNKNOWN, AuditEvent.LEGACY_MIGRATE}


def test_every_written_event_maps_back_after_a_downgrade():
    """The frozen mapping lists the (resource_type, action) pair every writer produces: a downgrade of 031 and an
    upgrade again map the rows written in between to their events"""
    for member in AuditEvent:
        if member in SERVED_ONLY:
            continue
        pair = audit_service.writer.resource_and_action(member)
        assert LEGACY_EVENTS.get(pair, (None,))[0] == member.code, (member.code, pair)


def test_details_params_name_the_params_of_their_event():
    """031 copies old details keys of these events into params: every target is a param of the event, and every
    required param has a source key"""
    for code, names in DETAILS_PARAMS.items():
        member = audit_event(code)
        assert set(names.values()) <= set(member.params), code
        required = {name for name, annotation in member.params.items() if not is_optional(annotation)}
        assert required <= set(names.values()), code


def test_legacy_events_point_at_catalog_events():
    """The frozen mapping (migrations/support/audit_events_v1.py) names events of the catalog in their category"""
    for pair, (code, category) in LEGACY_EVENTS.items():
        member = audit_event(code)
        assert member is not None, pair
        assert member.category.value == category, pair
    for code, category in (LEGACY_UNKNOWN, LEGACY_MIGRATE):
        assert audit_event(code).category.value == category
