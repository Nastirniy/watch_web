"""TEMPORARY (step 6 of the api/ rework): the problems declared on the routes equal what the call graph finds.

Removed together with the call graph in step 7. The only accepted difference: validation_error on the
routes without inputs, which the version router now declares for all of its routes.
"""
from fastapi.routing import APIRoute

from app.backend.enums.errors import ErrorCode
from app.backend.openapi_problems import CallGraph, declared_problems, route_problems, route_validates
from app.main import fastapi_app


def test_declared_problems_equal_the_call_graph():
    graph = CallGraph()
    differences = []
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        declared = declared_problems(route.responses)
        found = route_problems(route, graph)
        extra = declared - found
        if not route_validates(route) and route.path_format.startswith("/api/"):
            extra.discard(ErrorCode.VALIDATION_ERROR)
        missing = found - declared
        if extra or missing:
            differences.append(f"{route.name}: missing {sorted(c.value for c in missing)}, "
                               f"extra {sorted(c.value for c in extra)}")
    assert not differences, "\n".join(differences)
