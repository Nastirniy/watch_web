"""The db_backup command line: exit codes, confirmation, output (services patched; spec section 3)"""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.cli import db_backup
from app.backend.config import settings
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.carry_over import CarryOverResult
from app.backend.services.backups.dump import CreateResult
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, write_manifest
from app.backend.services.backups.restore import RestorePlan, RestoreRefused, RestoreResult
from app.backend.services.backups.schemas import KeptSchema

UTC = timezone.utc
STARTED = datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC)
CARRY = CarryOverResult(revocations=1, certificates_added=0, orphan_serials=(), crl_number=4, sessions_ended=2)


def manifest(started=STARTED, label=BackupLabel.SCHEDULED) -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, label), label=label, started_at=started,
        finished_at=started + timedelta(minutes=1), database="radio_monitoring", server_product="mariadb",
        server_version="11.8.6", dump_tool="mariadb-dump", migration_revision="028_no_actor_foreign_keys",
        tables=("t",), file="dump.sql.gz", size_bytes=2048, sha256="ab" * 32,
    )


class FakeBackend:
    def __init__(self):
        self.kept = [KeptSchema(name="radio_monitoring_before_20261021_100000",
                                created_at=datetime(2026, 10, 21, 10, tzinfo=UTC), size_bytes=5)]

    def kept_schemas(self, db):
        return self.kept

    def unsupported_objects(self, schema):
        return []


@pytest.fixture
def configured(tmp_path, monkeypatch):
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setattr(settings.backup, "dir", str(directory))
    monkeypatch.setattr(db_backup, "_backend", lambda: FakeBackend())
    monkeypatch.setattr(db_backup, "find_tools", lambda: None)
    return directory


def run(argv, stdin=""):
    import sys
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        return db_backup.main(argv)
    finally:
        sys.stdin = old


def test_not_configured_exit_2(monkeypatch, capsys):
    monkeypatch.setattr(settings.backup, "dir", "")
    assert run(["list"]) == 2
    assert "MYSETTINGS_BACKUP__DIR" in capsys.readouterr().err


def test_bad_usage_exit_2(configured):
    with pytest.raises(SystemExit) as exc:
        run(["nonsense"])
    assert exc.value.code == 2


def test_create_prints_id(configured, monkeypatch, capsys):
    seen = {}

    def fake_create(cfg, db, label, *, copy, object_guard, tools=None, **kw):
        seen.update(label=label, copy=copy, guard=object_guard())
        return CreateResult(manifest=manifest(label=label), copied=None, pruned=())

    monkeypatch.setattr(db_backup, "create_backup", fake_create)
    assert run(["create", "--label", "pre-upgrade", "--no-copy"]) == 0
    assert capsys.readouterr().out.strip() == "20261022T031500Z-pre-upgrade"
    assert seen == {"label": BackupLabel.PRE_UPGRADE, "copy": False, "guard": []}


def test_create_default_label_is_manual(configured, monkeypatch, capsys):
    monkeypatch.setattr(db_backup, "create_backup", lambda cfg, db, label, **kw: CreateResult(
        manifest=manifest(label=label), copied=True, pruned=()))
    assert run(["create"]) == 0
    assert capsys.readouterr().out.strip() == "20261022T031500Z-manual"


@pytest.mark.parametrize("code, exit_code, word", [("no_space", 3, "refused"), ("dump_failed", 1, "failed")])
def test_create_failures(configured, monkeypatch, capsys, code, exit_code, word):
    def failing(*args, **kw):
        raise BackupFailure(code)

    monkeypatch.setattr(db_backup, "create_backup", failing)
    assert run(["create"]) == exit_code
    assert capsys.readouterr().err.strip() == f"{word}: {code}"


def test_list_json(configured, capsys):
    older, newer = manifest(STARTED - timedelta(days=1)), manifest()
    for m in (older, newer):
        (configured / m.id).mkdir()
        write_manifest(configured / m.id, m)
    (configured / "20261020T000000Z-manual").mkdir()
    assert run(["list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [b["id"] for b in data["backups"]] == [newer.id, older.id]
    assert data["backups"][0]["started_at"] == "2026-10-22T03:15:00+00:00"
    assert data["incomplete"][0]["name"] == "20261020T000000Z-manual"


def test_list_text(configured, capsys):
    m = manifest()
    (configured / m.id).mkdir()
    write_manifest(configured / m.id, m)
    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert m.id in out and "028_no_actor_foreign_keys" in out


def test_status_json(configured, capsys):
    assert run(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["count"] == 0 and data["summary"]["stale"] is True
    assert data["previous_states"][0]["schema"] == "radio_monitoring_before_20261021_100000"


PLAN = RestorePlan(backup_id="20261022T031500Z-scheduled", label="scheduled", started_at=STARTED,
                   backup_revision="028_no_actor_foreign_keys", live_revision="028_no_actor_foreign_keys",
                   other_connections=0)
RESULT = RestoreResult(kept_schema="radio_monitoring_before_20261022_120000", carry=CARRY,
                       user_differences=[(3, "anna", "is_active True->False")])


@pytest.fixture
def restore_calls(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(db_backup, "plan_restore", lambda cfg, db, ref, backend: PLAN)

    def fake_restore(cfg, db, ref, *, backend, force_connections, skip_orphans):
        calls.append((ref, force_connections, skip_orphans))
        return RESULT

    monkeypatch.setattr(db_backup, "restore_backup", fake_restore)
    return calls


def test_restore_needs_yes(restore_calls, capsys):
    assert run(["restore", "latest"], stdin="no\n") == 3
    assert "aborted" in capsys.readouterr().err
    assert restore_calls == []
    assert run(["restore", "latest"], stdin="yes\n") == 0
    assert restore_calls == [("latest", False, False)]


def test_restore_with_flags(restore_calls, capsys):
    assert run(["restore", "20261022T031500Z-scheduled", "--yes", "--force-connections", "--force-orphans"]) == 0
    assert restore_calls == [("20261022T031500Z-scheduled", True, True)]
    out = capsys.readouterr().out
    assert "radio_monitoring_before_20261022_120000" in out
    assert "anna" in out and "is_active True->False" in out
    assert "docker compose up -d" in out


def test_restore_busy(configured, monkeypatch, capsys):
    monkeypatch.setattr(db_backup, "plan_restore", lambda cfg, db, ref, backend: PLAN)

    def busy(*args, **kw):
        raise BackupFailure("busy")

    monkeypatch.setattr(db_backup, "restore_backup", busy)
    assert run(["restore", "latest", "--yes"]) == 3
    assert capsys.readouterr().err.strip() == "refused: busy"


def test_restore_refused_lists_orphans(configured, monkeypatch, capsys):
    monkeypatch.setattr(db_backup, "plan_restore", lambda cfg, db, ref, backend: PLAN)

    def orphans(*args, **kw):
        raise RestoreRefused("orphan_certificates", serials=("abc1", "abc2"))

    monkeypatch.setattr(db_backup, "restore_backup", orphans)
    assert run(["restore", "latest", "--yes"]) == 3
    err = capsys.readouterr().err
    assert "refused: orphan_certificates" in err and "abc1" in err and "abc2" in err


def test_restore_prints_crl_step_only_with_pki(restore_calls, capsys, monkeypatch):
    run(["restore", "latest", "--yes"])
    assert "app.backend.cli.pki crl" not in capsys.readouterr().out
    monkeypatch.setattr(type(settings.pki), "is_configured", lambda self: True)
    run(["restore", "latest", "--yes"])
    assert "app.backend.cli.pki crl" in capsys.readouterr().out


def test_undo_restore(configured, monkeypatch, capsys):
    seen = []

    def fake_undo(cfg, db, schema, *, backend, force_connections, skip_orphans):
        seen.append(schema)
        return RESULT

    monkeypatch.setattr(db_backup, "undo_restore", fake_undo)
    assert run(["undo-restore"], stdin="yes\n") == 0
    assert run(["undo-restore", "radio_monitoring_before_20261021_100000", "--yes"]) == 0
    assert seen == [None, "radio_monitoring_before_20261021_100000"]


def test_prune(configured, monkeypatch, capsys):
    monkeypatch.setattr(db_backup, "prune", lambda cfg, now, dry_run: ["20261001T031500Z-scheduled"])
    monkeypatch.setattr(db_backup, "prune_kept_schemas", lambda cfg, db, backend, now, dry_run: ["s_before_x"])
    assert run(["prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "20261001T031500Z-scheduled" in out and "s_before_x" in out and "would delete" in out


def test_verify(configured, monkeypatch, capsys):
    from app.backend.services.backups.restore import VerifyResult
    monkeypatch.setattr(db_backup, "verify_backup", lambda cfg, db, ref, deep, backend: VerifyResult(
        backup_id="x", checksum_ok=True, gzip_ok=True, tables_ok=False, revision_ok=True))
    assert run(["verify", "latest", "--deep"]) == 1
    assert "tables: mismatch" in capsys.readouterr().out


def test_output_has_no_password(configured, monkeypatch, capsys):
    monkeypatch.setattr(settings.database, "password", "pw-that-must-not-leak")
    monkeypatch.setattr(db_backup, "create_backup", lambda *a, **kw: CreateResult(
        manifest=manifest(), copied=None, pruned=()))
    for argv in (["create"], ["list"], ["status"], ["status", "--json"]):
        run(argv)
        captured = capsys.readouterr()
        assert "pw-that-must-not-leak" not in captured.out + captured.err
