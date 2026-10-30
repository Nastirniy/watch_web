"""Every free-text field follows one rule, the natural plain text of SecurityValidators.plain_text (decision
2026-10-30): length limits, no control characters (single-line fields also refuse the line separators and the bidi
controls), no script patterns, and no keyword list. SQL words are ordinary words: every value is a bound parameter.
"""
import pytest
from pydantic import ValidationError

from app.backend.api.v1.models.request.categories import CategoryCreate, CategoryUpdate
from app.backend.api.v1.models.request.corps import CorpsCreate
from app.backend.api.v1.models.request.media_formats import MediaFormatUpdate
from app.backend.api.v1.models.request.observations import ObservationUpdate
from app.backend.api.v1.models.request.posts import PostCreate, PostUpdate
from app.backend.api.v1.models.request.roles import RoleCreate, RoleUpdate
from app.backend.api.v1.models.request.scope_groups import ScopeGroupCreate
from app.backend.api.v1.models.request.signal_types import SignalTypeCreate, SignalTypeUpdate
from app.backend.api.v1.models.request.tags import TagCreate, TagUpdate
from app.backend.api.v1.models.request.users import UserCreateData, UserUpdateData
from app.backend.enums.errors import FieldErrorCode
from app.backend.utils.validators import SecurityValidators

SQL_WORDS = "Select, create, update, insert, delete, drop, alter, exec, union and script"
MULTILINE = "Select the post.\nThen create\tthe observation and drop the band."

# (model, base body, single-line text fields, multi-line text fields)
MODELS = [
    (PostCreate, {"name": "n", "code": "c"}, ["name", "code", "region", "city"], ["description"]),
    (PostUpdate, {}, ["name", "code", "region", "city"], ["description"]),
    (TagCreate, {"name": "n"}, ["name"], []),
    (TagUpdate, {}, ["name"], []),
    (CategoryCreate, {"name": "n", "alias": "a"}, ["name", "alias"], []),
    (CategoryUpdate, {}, ["name", "alias"], []),
    (SignalTypeCreate, {"code": "c", "name": "n"}, ["name"], ["description"]),
    (SignalTypeUpdate, {}, ["name"], ["description"]),
    (ScopeGroupCreate, {"name": "n"}, ["name"], ["description"]),
    (CorpsCreate, {"name": "n"}, ["name"], ["description"]),
    (RoleCreate, {"name": "n", "display_name": "d", "kind": "tree"}, ["name", "display_name"], ["description"]),
    (RoleUpdate, {}, ["name", "display_name"], ["description"]),
    (UserCreateData, {"username": "ivan_k", "username_alias": "a", "user_type": "viewer"}, ["username_alias"], []),
    (UserUpdateData, {}, ["username_alias"], []),
    (MediaFormatUpdate, {}, ["label"], []),
    (ObservationUpdate, {}, [], ["comment"]),
]


def _codes(exc: ValidationError):
    return [error["type"] for error in exc.errors()]


def _cases():
    for model, base, single, multi in MODELS:
        for field in single + multi:
            yield pytest.param(model, base, field, field in multi, id=f"{model.__name__}.{field}")


@pytest.mark.parametrize("model, base, field, multiline", list(_cases()))
def test_sql_words_are_ordinary_words(model, base, field, multiline):
    text = SQL_WORDS[:50] if field == "code" else SQL_WORDS
    value = getattr(model(**{**base, field: text}), field)
    assert value.lower() == text.lower()
    if multiline:
        assert getattr(model(**{**base, field: MULTILINE}), field) == MULTILINE


@pytest.mark.parametrize("model, base, field, multiline", list(_cases()))
def test_script_patterns_and_control_characters_are_refused(model, base, field, multiline):
    with pytest.raises(ValidationError) as script:
        model(**{**base, field: "x <script>alert(1)</script>"})
    assert FieldErrorCode.TEXT_SCRIPT_PATTERN.value in _codes(script.value)
    with pytest.raises(ValidationError) as control:
        model(**{**base, field: "bad\x07text"})
    assert FieldErrorCode.TEXT_CONTROL_CHARACTER.value in _codes(control.value)
    if not multiline:
        for text in ("two\nlines", "invoice‮txt"):
            with pytest.raises(ValidationError) as hidden:
                model(**{**base, field: text})
            assert FieldErrorCode.TEXT_CONTROL_CHARACTER.value in _codes(hidden.value)


def test_the_keyword_rule_is_gone():
    assert "TEXT_SQL_PATTERN" not in FieldErrorCode.__members__
    assert "sql_injection" not in SecurityValidators.PATTERNS
    assert not hasattr(SecurityValidators, "sanitize_string")
