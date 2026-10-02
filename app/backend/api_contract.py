"""The API contract of the web client: export it, lock the error part, check the code against the lock.

    python -m app.backend.api_contract export --out <dir>   openapi.json + problems.json for the web tooling
    python -m app.backend.api_contract check                compare the code with api-contract/problems.lock.json
    python -m app.backend.api_contract lock                 accept the current contract (commit the lock with the change)

The problems of each operation are derived from the code (openapi_problems.py), which keeps /docs honest
but would let a refactoring change the contract silently: a code string renamed, a param renamed or
retyped, a code no longer found for an operation. The lock file makes every such change visible in the
diff of the pull request and fails tests/test_api_contract.py until it is accepted with "lock".
The lock names codes and params only, never Python functions or files: moving or renaming code that keeps
the same codes changes nothing.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Union, get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "api-contract" / "problems.lock.json"


# ============================================================ building the contract

def type_label(annotation: Any) -> str:
    """Stable, readable type of a param: int, str, list[str], RequiredPermissions, ..."""
    origin = get_origin(annotation)
    if origin is Union:
        return " | ".join(type_label(arg) for arg in get_args(annotation) if arg is not type(None))
    if origin in (list, List):
        return f"list[{type_label(get_args(annotation)[0])}]"
    if origin in (dict, Dict):
        key, value = get_args(annotation)
        return f"dict[{type_label(key)}, {type_label(value)}]"
    if annotation is Any:
        return "any"
    return getattr(annotation, "__name__", str(annotation))


def _params(code) -> Dict[str, Dict[str, Any]]:
    from app.backend.services.api_errors import is_optional
    return {name: {"type": type_label(annotation), "required": not is_optional(annotation)}
            for name, annotation in code.params.items()}


def builtin_field_errors(own_codes) -> Dict[str, Any]:
    """Codes pydantic uses for its built-in checks (missing, string_too_short, ...), with their English
    template and params; the codes of the API take precedence where a name is the same"""
    from pydantic_core._pydantic_core import list_all_errors
    from app.backend.error_handlers import DROPPED_CONTEXT
    # Params the API really sends: context it drops (the error object, the input) is not one of them
    return {error["type"]: {"detail": error["message_template_python"],
                            "params": sorted(k for k in (error["example_context"] or {}) if k not in DROPPED_CONTEXT)}
            for error in sorted(list_all_errors(), key=lambda e: e["type"]) if error["type"] not in own_codes}


def build_catalog() -> Dict[str, Any]:
    """Every problem type and field error code with what the web client relies on (the localization keys)"""
    from app.backend.enums.errors import PROBLEM_ENUMS, FieldErrorCode
    field_errors = {code.value: {"detail": code.detail_template, "params": _params(code)} for code in FieldErrorCode}
    return {
        "problems": {code.value: {"status": code.status, "title": code.title, "detail": code.detail_template,
                                  "params": _params(code)}
                     for cls in PROBLEM_ENUMS for code in cls},
        "field_errors": field_errors,
        # Translating these is optional: the item's "detail" is the English fallback
        "builtin_field_errors": builtin_field_errors(field_errors.keys()),
    }


def build_operations(app) -> Dict[str, Any]:
    """operationId -> route and the problem codes it answers per status"""
    from fastapi.routing import APIRoute
    from app.backend.openapi_problems import CallGraph, route_problems
    graph = CallGraph()
    operations = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        by_status: Dict[str, List[str]] = {}
        for code in sorted(route_problems(route, graph), key=lambda c: (c.status, c.value)):
            by_status.setdefault(str(code.status), []).append(code.value)
        operations[route.unique_id] = {"route": f"{','.join(sorted(route.methods))} {route.path_format}", "problems": by_status}
    return dict(sorted(operations.items()))


def build_lock(app) -> Dict[str, Any]:
    """What a change must not alter unnoticed: codes, their status and params, the codes of each operation.
    Titles and detail texts are English fallbacks and stay out of the lock."""
    catalog = build_catalog()
    return {
        "problems": {code: {"status": spec["status"], "params": spec["params"]} for code, spec in catalog["problems"].items()},
        "field_errors": {code: {"params": spec["params"]} for code, spec in catalog["field_errors"].items()},
        "operations": build_operations(app),
    }


# ============================================================ comparing

def diff_lock(locked: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, List[str]]:
    """Changes between the lock and the code, split into what breaks the web client and what it must add"""
    breaking: List[str] = []
    additive: List[str] = []

    for section, label in (("problems", "problem"), ("field_errors", "field error")):
        old, new = locked.get(section, {}), current[section]
        for code in sorted(old.keys() - new.keys()):
            breaking.append(f"{label} code removed or renamed: {code} (its translation key disappears)")
        for code in sorted(new.keys() - old.keys()):
            additive.append(f"{label} code added: {code} (needs a translation)")
        for code in sorted(old.keys() & new.keys()):
            if old[code].get("status") != new[code].get("status"):
                breaking.append(f"{code}: status {old[code].get('status')} -> {new[code].get('status')}")
            old_params, new_params = old[code]["params"], new[code]["params"]
            for name in sorted(old_params.keys() - new_params.keys()):
                breaking.append(f"{code}: param removed or renamed: {name}")
            for name in sorted(new_params.keys() - old_params.keys()):
                (breaking if new_params[name]["required"] else additive).append(
                    f"{code}: param added: {name} ({new_params[name]['type']})")
            for name in sorted(old_params.keys() & new_params.keys()):
                before, after = old_params[name], new_params[name]
                if before["type"] != after["type"]:
                    breaking.append(f"{code}.{name}: type {before['type']} -> {after['type']}")
                if before["required"] and not after["required"]:
                    breaking.append(f"{code}.{name}: no longer always present")

    old_ops, new_ops = locked.get("operations", {}), current["operations"]
    for op in sorted(old_ops.keys() - new_ops.keys()):
        breaking.append(f"operation removed or renamed: {op} ({old_ops[op]['route']})")
    for op in sorted(new_ops.keys() - old_ops.keys()):
        additive.append(f"operation added: {op} ({new_ops[op]['route']})")
    for op in sorted(old_ops.keys() & new_ops.keys()):
        old_codes = {(s, c) for s, codes in old_ops[op]["problems"].items() for c in codes}
        new_codes = {(s, c) for s, codes in new_ops[op]["problems"].items() for c in codes}
        for status, code in sorted(old_codes - new_codes):
            breaking.append(f"{op}: no longer answers {status} {code} (a refactoring the call graph cannot follow, "
                            f"or a real change; the web client's handling of it stops compiling)")
        for status, code in sorted(new_codes - old_codes):
            additive.append(f"{op}: now answers {status} {code} (the web client must handle it)")
        if old_ops[op]["route"] != new_ops[op]["route"]:
            breaking.append(f"{op}: route {old_ops[op]['route']} -> {new_ops[op]['route']}")
    return {"breaking": breaking, "additive": additive}


def report(changes: Dict[str, List[str]]) -> str:
    lines = []
    if changes["breaking"]:
        lines.append("BREAKING for the web client (localization keys, params or handled codes):")
        lines += [f"  - {line}" for line in changes["breaking"]]
    if changes["additive"]:
        lines.append("Additions the web client must pick up:")
        lines += [f"  - {line}" for line in changes["additive"]]
    if lines:
        lines.append("If intended: python -m app.backend.api_contract lock, commit api-contract/problems.lock.json, "
                     "note it in CHANGELOG.md and tell the web team (breaking changes need their release).")
    return "\n".join(lines)


def read_lock() -> Dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8")) if LOCK_PATH.exists() else {}


def write_json(path: Path, data: Any, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


# ============================================================ command line

def _app():
    from app.main import fastapi_app
    return fastapi_app


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backend.api_contract", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write openapi.json and problems.json for the web tooling")
    export.add_argument("--out", type=Path, default=REPO_ROOT / "api-contract" / "export")
    commands.add_parser("check", help="compare the code with the lock; exit 1 on any change")
    commands.add_parser("lock", help="accept the current contract into the lock file")
    args = parser.parse_args(argv)

    app = _app()
    if args.command == "export":
        write_json(args.out / "openapi.json", app.openapi())
        write_json(args.out / "problems.json", build_catalog())
        print(f"wrote {args.out / 'openapi.json'} and {args.out / 'problems.json'}")
        return 0
    current = build_lock(app)
    if args.command == "lock":
        write_json(LOCK_PATH, current)
        print(f"wrote {LOCK_PATH}")
        return 0
    changes = diff_lock(read_lock(), current)
    text = report(changes)
    print(text or "api contract unchanged")
    return 1 if text else 0


if __name__ == "__main__":
    sys.exit(main())
