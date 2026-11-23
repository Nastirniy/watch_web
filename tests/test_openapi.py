"""The OpenAPI document of v1 (/api/v1/openapi.json, /api/v1/docs) documents every response the API really gives.

- every error response is application/problem+json and names its problem types (oneOf + discriminator on code);
- the problem types of a route are those its decorator declares (responses=problems(...)), merged by FastAPI with
  the app (internal_error) and the version router (401 set, validation_error); tests/conftest.py checks that every
  problem the suite makes the app answer is declared;
- a real error body validates against the schema of its problem type (the documentation is 1:1);
- no success response is documented as JSON without a schema.
"""
import json

import pytest

from app.backend.enums.errors import PROBLEM_ENUMS, AccessErrorCode, AuthErrorCode, ErrorCode, FieldErrorCode
from app.backend.enums.routes import APIVersionRoutes, PostRoutes, TagRoutes
from app.backend.api.v1 import V1
from app.backend.errors.openapi import FRAMEWORK_PROBLEMS, problem_models, schema_name
from app.backend.errors.problems import PROBLEM_JSON
from app.backend.auth.require import Require
from tests.helpers import make_admin, make_user

# Answered by the framework outside any operation
GLOBAL_PROBLEMS = set(FRAMEWORK_PROBLEMS)
API_PREFIXES = tuple(version.value + "/" for version in APIVersionRoutes)


@pytest.fixture(scope="module")
def schema():
    from app.main import fastapi_app
    return V1.openapi(fastapi_app)


def _operations(schema):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            yield f"{method.upper()} {path}", operation


def _codes(response) -> set:
    media = response["content"][PROBLEM_JSON]["schema"]
    refs = media.get("oneOf", [media])
    return {ref["$ref"].rsplit("/", 1)[1] for ref in refs}


def _names(*codes) -> set:
    return {schema_name(code) for code in codes}


def test_fastapi_validation_schemas_are_replaced(schema):
    components = schema["components"]["schemas"]
    assert "HTTPValidationError" not in components and "ValidationError" not in components
    assert "FieldError" in components
    validation = components[schema_name(ErrorCode.VALIDATION_ERROR)]
    assert validation["properties"]["errors"]["items"]["$ref"].endswith("/FieldError")


def test_error_responses_are_problems(schema):
    for name, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if int(status) < 400:
                continue
            assert list(response["content"]) == [PROBLEM_JSON], f"{name} {status}"
            for code in _codes(response):
                component = schema["components"]["schemas"][code]
                assert component["properties"]["status"]["const"] == int(status), f"{name}: {code} under {status}"
        assert "500" in operation["responses"], f"{name} does not document internal_error"


def test_no_success_response_is_json_without_a_schema(schema):
    for name, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if int(status) >= 400:
                continue
            for media_type, media in response.get("content", {}).items():
                assert media.get("schema"), f"{name} {status} {media_type}: no schema"


def test_authentication_and_permissions_follow_require(schema):
    from app.main import fastapi_app
    from fastapi.routing import APIRoute
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(API_PREFIXES) or not route.include_in_schema:
            continue
        spec = next((d.call for d in route.dependant.dependencies if isinstance(d.call, Require)), None)
        if spec is None:
            continue
        for method in route.methods:
            responses = schema["paths"][route.path_format][method.lower()]["responses"]
            assert _names(AuthErrorCode.CREDENTIALS_REQUIRED, AuthErrorCode.INVALID_TOKEN) <= _codes(responses["401"]), route.path
            forbidden = _codes(responses["403"]) if "403" in responses else set()
            if spec.all or spec.any or spec.at_root:
                assert schema_name(AccessErrorCode.PERMISSION_DENIED) in forbidden, route.path


def test_route_problems_come_from_its_declaration(schema):
    update_post = schema["paths"][V1.path(PostRoutes.POST_BY_ID)]["put"]["responses"]
    assert _codes(update_post["404"]) == _names(ErrorCode.POST_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_FOUND)
    assert _codes(update_post["400"]) == _names(ErrorCode.POST_CODE_TAKEN)
    assert _codes(update_post["422"]) == _names(ErrorCode.VALIDATION_ERROR, ErrorCode.MOVE_NOT_ALONE)
    assert "415" not in update_post


def test_every_problem_type_is_documented_somewhere(schema):
    documented = set()
    for _, operation in _operations(schema):
        for status, response in operation["responses"].items():
            if int(status) >= 400:
                documented |= _codes(response)
    for cls in PROBLEM_ENUMS:
        for code in cls:
            if code not in GLOBAL_PROBLEMS:
                assert schema_name(code) in documented, f"{code.value} is answered by no route: remove it or raise it"


def test_catalog_components(schema):
    """Every problem type and every field error code is in the document: the web client types its
    translations from them (WEB_TOOLING.md); nothing is injected besides the framework problems"""
    components = schema["components"]["schemas"]
    every = [code for cls in PROBLEM_ENUMS for code in cls]
    assert {schema_name(code) for code in every} <= set(components)
    assert components["FieldErrorCode"]["enum"] == [code.value for code in FieldErrorCode]
    assert "Problem" not in components and "ProblemCode" not in components
    post_not_found = components[schema_name(ErrorCode.POST_NOT_FOUND)]
    assert post_not_found["properties"]["code"]["const"] == "post_not_found"
    assert set(post_not_found["required"]) == {"type", "title", "status", "detail", "instance", "code"}
    assert post_not_found["additionalProperties"] is False


def _reachable(schema, node, found: set) -> set:
    """Names of the components `node` refers to, directly or through other components"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                name = value.rsplit("/", 1)[-1]
                if name not in found:
                    found.add(name)
                    _reachable(schema, schema["components"]["schemas"][name], found)
            else:
                _reachable(schema, value, found)
    elif isinstance(node, list):
        for value in node:
            _reachable(schema, value, found)
    return found


def test_response_fields_with_a_default_are_required(schema):
    """The web generates its types with openapi-typescript --default-non-nullable false (WEB_TOOLING.md): a response
    field with a default must be in `required` or the client types it as maybe missing. Optional in a response means
    left out of the body (absent_not_null: no default, no null variant). A new response model takes a base with
    utils/validators.SERIALIZED_DEFAULTS_REQUIRED (ResponseModel, PlainTextModel, SecureBaseModel)"""
    names = set()
    for _, operation in _operations(schema):
        _reachable(schema, operation["responses"], names)
    wrong = []
    for name in sorted(names):
        component = schema["components"]["schemas"][name]
        required = set(component.get("required", ()))
        for field, prop in component.get("properties", {}).items():
            if field not in required and ("default" in prop or {"type": "null"} in prop.get("anyOf", [])):
                wrong.append(f"{name}.{field}")
    assert wrong == []
    # The setting acts in responses only: a request field with a default stays optional, and no model is split into
    # an -Input and an -Output component
    post_update = schema["components"]["schemas"]["PostUpdate"]
    assert "force" not in post_update.get("required", ())
    assert [name for name in schema["components"]["schemas"] if name.endswith(("-Input", "-Output"))] == []


# ------------------------------------------------------------------ real bodies match their schema

def test_operation_ids_are_unique_function_names(schema):
    ids = [operation["operationId"] for _, operation in _operations(schema)]
    assert len(ids) == len(set(ids))
    assert schema["paths"][V1.path(PostRoutes.POST_BY_ID)]["put"]["operationId"] == "update_post"


def test_component_names_carry_no_module_path(schema):
    """Two models of one name and different fields make FastAPI name BOTH components after their module path
    (app__backend__api__v1__...__UserResponse), which renames the types the web client uses"""
    assert [name for name in schema["components"]["schemas"] if "__" in name] == []


def test_optional_members_are_absent_not_null(schema):
    """Bodies leave an optional member out; the schema must not type it as a present nullable value"""
    post_not_found = schema["components"]["schemas"][schema_name(ErrorCode.POST_NOT_FOUND)]
    assert post_not_found["properties"]["post_id"] == {"type": "integer", "title": "Post Id"}
    assert "post_id" not in post_not_found["required"]


def _validate(response):
    body = response.json()
    assert all(value is not None for value in body.values()), f"null member in {body}"
    code = next(code for cls in PROBLEM_ENUMS for code in cls if code.value == body["code"])
    problem_models()[code].model_validate(body)
    return body


@pytest.mark.asyncio
async def test_real_bodies_validate_against_their_problem_schema(client, db, headers):
    admin = await make_admin(db)
    viewer = await make_user(db, perms=["tag.view"])

    _validate(await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=999999), headers=headers(admin)))
    _validate(await client.get(V1.path(PostRoutes.ROOT)))
    denied = _validate(await client.post(V1.path(TagRoutes.CREATE), json={"name": "x"}, headers=headers(viewer)))
    assert denied["required"]["all"] and denied["missing"]
    invalid = _validate(await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=1), json={"code": None}, headers=headers(admin)))
    assert invalid["errors"][0]["code"] == "value_null"


def test_openapi_is_json_serialisable_and_compact(schema):
    size = len(json.dumps(schema, separators=(",", ":")))
    assert size < 2_000_000, f"openapi.json is {size} bytes"


def test_the_stream_of_events_is_one_named_message_schema(schema):
    """FastAPI writes {"type": "string"} next to the model of a non-JSON class; openapi-typescript would give
    string & (union). The media type holds only the $ref (errors/openapi.py _finish)"""
    from app.backend.enums.media_types import ContentType
    from app.backend.enums.routes import EventRoutes
    operation = schema["paths"][V1.path(EventRoutes.STREAM)]["get"]
    media = operation["responses"]["200"]["content"]
    assert list(media) == [ContentType.EVENT_STREAM.value]
    assert media[ContentType.EVENT_STREAM.value]["schema"] == {"$ref": "#/components/schemas/StreamMessage"}
    assert "content" not in operation["responses"]["204"]
    assert "EventStreamCapacityProblem" in _codes(operation["responses"]["503"])
    message = schema["components"]["schemas"]["StreamMessage"]
    assert message["discriminator"]["propertyName"] == "type"
