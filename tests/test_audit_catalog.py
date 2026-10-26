"""The audit event catalog (enums/audit.py): codes, categories, params"""
import re
from typing import get_args

from app.backend.enums.audit import (
    SWITCHABLE, AuditCategory, AuditEvent, EventSpec, audit_event, event,
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
