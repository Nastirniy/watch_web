"""Documentation of the problem types: what the "type" URI of every error (RFC 9457) dereferences to.

GET /problems            index of every problem type (HTML)
GET /problems/{code}     one problem type: status, title, detail template, params, routes that answer it

Public and outside the versions: it describes the API, never data. Not part of the OpenAPI documents; the
same catalog is there as component schemas (see errors/openapi.py).
"""
import html
from typing import Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.backend.api.versioning import PublishedVersion
from app.backend.enums.errors import PROBLEM_ENUMS, ErrorCode, ProblemType, problem_type
from app.backend.enums.routes import SystemRoutes
from app.backend.errors.openapi import operation_problems
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import is_optional

router = APIRouter(include_in_schema=False)

_STYLE = """
body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1b1f24;background:#fff}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #d8dee4;vertical-align:top}
code{background:#f3f5f7;padding:0 .25rem;border-radius:3px}a{color:#0b5cad}h1{font-size:1.4rem}.muted{color:#57606a}
@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}th,td{border-color:#30363d}code{background:#161b22}a{color:#58a6ff}.muted{color:#8b949e}}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
                        f"content=\"width=device-width,initial-scale=1\"><title>{html.escape(title)}</title>"
                        f"<style>{_STYLE}</style></head><body>{body}</body></html>")


def _type_name(annotation) -> str:
    return html.escape(str(annotation).replace("typing.", "").replace("app.backend.enums.errors.", ""))


def _routes_by_problem(request: Request) -> Dict[str, List[str]]:
    """code -> "METHOD /path" of the operations that declare it in the OpenAPI document of any version"""
    found: Dict[str, List[str]] = {}
    for version in PublishedVersion.published.values():
        for path, path_item in version.openapi(request.app)["paths"].items():
            for method, operation in path_item.items():
                for codes in operation_problems(operation).values():
                    for code in codes:
                        route = f"{method.upper()} {path}"
                        if route not in found.setdefault(code, []):
                            found[code].append(route)
    return found


@router.get(SystemRoutes.PROBLEMS.value, response_class=HTMLResponse)
async def list_problem_types(request: Request) -> HTMLResponse:
    rows = []
    for cls in PROBLEM_ENUMS:
        for code in cls:
            link = request.url_for("get_problem_type", code=code.value).path
            rows.append(f"<tr><td><a href=\"{link}\"><code>{code.value}</code></a></td><td>{code.status}</td>"
                        f"<td>{html.escape(code.title)}</td></tr>")
    body = (f"<h1>Problem types</h1><p class=\"muted\">Every error of the API is an RFC 9457 problem "
            f"(<code>application/problem+json</code>). <code>code</code> is the localization key of exactly one "
            f"message; <code>type</code> points to its page here.</p>"
            f"<table><thead><tr><th>code</th><th>status</th><th>title</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")
    return _page("Problem types", body)


@router.get(SystemRoutes.PROBLEM_BY_CODE.value, response_class=HTMLResponse)
async def get_problem_type(code: str, request: Request) -> HTMLResponse:
    problem: ProblemType = problem_type(code)
    if problem is None:
        raise ApiError(ErrorCode.PROBLEM_TYPE_NOT_FOUND, problem_code=code)
    params = "".join(
        f"<tr><td><code>{html.escape(name)}</code></td><td>{_type_name(annotation)}</td>"
        f"<td>{'optional' if is_optional(annotation) else 'always'}</td></tr>"
        for name, annotation in problem.params.items()
    ) or "<tr><td colspan=\"3\" class=\"muted\">none</td></tr>"
    routes = _routes_by_problem(request).get(problem.value, [])
    route_list = "".join(f"<li><code>{html.escape(r)}</code></li>" for r in routes) or "<li class=\"muted\">none</li>"
    index = request.url_for("list_problem_types").path
    body = (f"<p><a href=\"{index}\">Problem types</a></p><h1><code>{problem.value}</code></h1>"
            f"<table><tbody><tr><th>status</th><td>{problem.status}</td></tr>"
            f"<tr><th>title</th><td>{html.escape(problem.title)}</td></tr>"
            f"<tr><th>detail</th><td><code>{html.escape(problem.detail_template)}</code></td></tr></tbody></table>"
            f"<h2>Params (extension members)</h2><table><thead><tr><th>name</th><th>type</th><th>present</th></tr></thead>"
            f"<tbody>{params}</tbody></table><h2>Answered by</h2><ul>{route_list}</ul>")
    return _page(problem.value, body)
