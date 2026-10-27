"""One writer of audit rows: create_audit_log(session, AuditEvent.X, params=...). An AST scan of app/backend fails on
the legacy keywords and on a raw INSERT into audit_logs outside the places that are raw by design."""
import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"
LEGACY_KEYWORDS = {"action", "resource_type", "details"}
# raw INSERTs into audit_logs: migrations (frozen) and the restore tool (a swapped-in schema, not the live session)
RAW_INSERT_ALLOWED = (("migrations",), ("services", "backups", "restore.py"))
INSERT_SQL = re.compile(r"insert\s+(?:ignore\s+)?into\s+`?audit_logs", re.IGNORECASE)


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def legacy_calls(source: str) -> list[int]:
    """Lines of create_audit_log calls that pass action= / resource_type= / details="""
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and _call_name(node) == "create_audit_log"
            and any(keyword.arg in LEGACY_KEYWORDS for keyword in node.keywords)]


def _names_audit_table(node: ast.AST) -> bool:
    """AuditLog, AuditLog.__table__ or the literal "audit_logs" (also inside table("audit_logs"))"""
    for part in ast.walk(node):
        if isinstance(part, ast.Name) and part.id == "AuditLog":
            return True
        if isinstance(part, ast.Constant) and part.value == "audit_logs":
            return True
    return False


def raw_audit_inserts(source: str) -> list[int]:
    """Lines of insert(<audit_logs table>) calls and of INSERT INTO audit_logs strings"""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _call_name(node) == "insert" and node.args \
                and _names_audit_table(node.args[0]):
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and INSERT_SQL.search(node.value):
            lines.append(node.lineno)
    return lines


def _sources(*, skip: tuple[tuple[str, ...], ...]):
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).parts
        if any(relative[:len(prefix)] == prefix for prefix in skip):
            continue
        yield path, path.read_text(encoding="utf-8")


def test_the_scan_finds_planted_cases():
    """The scan is not vacuous: planted legacy calls and raw inserts are found, the event form is not"""
    assert legacy_calls("await create_audit_log(s, action='x', resource_type='y')") == [1]
    assert legacy_calls("await audit.create_audit_log(session=s, details={})") == [1]
    assert legacy_calls("await create_audit_log(s, AuditEvent.TAG_CREATE, params={'name': 'x'})") == []
    assert legacy_calls("other(action='x')") == []
    assert raw_audit_inserts("conn.execute(sa.insert(AuditLog.__table__).values(a=1))") == [1]
    assert raw_audit_inserts("insert(table('audit_logs'))") == [1]
    assert raw_audit_inserts("text('INSERT INTO audit_logs (a) VALUES (1)')") == [1]
    assert raw_audit_inserts("insert(ObservationUnit)") == []


def test_no_writer_passes_an_action_string():
    found = [f"{path.relative_to(BACKEND)}:{line}"
             for path, source in _sources(skip=(("migrations",),)) for line in legacy_calls(source)]
    assert not found, ("create_audit_log takes an AuditEvent and params, not action= / resource_type= / details=: "
                       f"{found}")


def test_no_raw_insert_into_audit_logs_outside_migrations_and_restore():
    found = [f"{path.relative_to(BACKEND)}:{line}"
             for path, source in _sources(skip=RAW_INSERT_ALLOWED) for line in raw_audit_inserts(source)]
    assert not found, f"audit rows are written by create_audit_log: {found}"


def test_services_do_not_import_the_frozen_legacy_mapping():
    for path, source in _sources(skip=(("migrations",),)):
        assert "audit_events_v1" not in source, str(path.relative_to(BACKEND))
