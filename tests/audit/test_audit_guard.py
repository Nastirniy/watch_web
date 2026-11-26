"""One writer of audit rows: create_audit_log(session, AuditEvent.X, params=...). An AST scan of app/backend fails on
the legacy keywords and on a raw INSERT into audit_logs outside the places that are raw by design. The params of every
row the tests write are checked against the v1 params models (tests/conftest.py audit_params_are_served); the raw
restore rows are checked here."""
import ast
import re
from types import SimpleNamespace

import pytest

from app.backend.enums.audit import AuditEvent
from app.backend.services.audit.writer import check_event_params
from app.backend.services.backups.carry_over import CarryOverResult
from app.backend.services.backups.restore import restore_params, undo_restore_params
from tests.paths import BACKEND_DIR
from tests.helpers import audit_params_mismatch

BACKEND = BACKEND_DIR
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


def test_the_params_guard_finds_a_mismatch():
    """The session guard is not vacuous: a value of the wrong type or a None where a str is required is found"""
    assert audit_params_mismatch(AuditEvent.TAG_CREATE, {"tag_name": "Red"}) is None
    assert audit_params_mismatch(AuditEvent.TAG_CREATE, {"tag_name": 5}) is not None
    assert audit_params_mismatch(AuditEvent.USER_CERTIFICATE_REVOKE,
                                 {"username": "u", "certificate_label": None}) is not None
    assert audit_params_mismatch(AuditEvent.USER_CERTIFICATE_REVOKE, {"username": "u", "certificate_label": ""}) is None


CARRY = CarryOverResult(revocations=1, certificates_added=2, users_added=((5, "u5"),), crl_number=7, sessions_ended=3)
MANIFEST = SimpleNamespace(id="20261022-120000-scheduled", label=SimpleNamespace(value="scheduled"),
                           migration_revision="031_audit_events")


@pytest.mark.parametrize("event,params", [
    (AuditEvent.DATABASE_RESTORE, restore_params(MANIFEST, CARRY, "db_before_x", 4)),
    (AuditEvent.DATABASE_UNDO_RESTORE, undo_restore_params("db_before_y", CARRY, "db_before_x", 4)),
], ids=["restore", "undo_restore"])
def test_restore_rows_fit_the_catalog(event, params):
    """The restore tool writes its row with a raw INSERT (services/backups/restore.py), past create_audit_log: its
    params must still be exactly the event's and validate against the v1 model"""
    check_event_params(event, params)
    assert audit_params_mismatch(event, params) is None
    assert params["added_user_count"] == 1 and params["raised_id_count"] == 4
