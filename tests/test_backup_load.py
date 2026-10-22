"""Backups: loading a dump never leaves the staging schema (edge plan S2)"""
import gzip

import pytest

from app.backend.config import DatabaseConfig
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.restore import MariaDbBackend, unsafe_statement
from tests.backup_fakes import DUMP_BODY, Fakes

DB = DatabaseConfig(name="radio_monitoring", user="u", password="p")


def test_access_denied_on_a_schema_is_no_grant():
    """A 1044 from DROP / CREATE DATABASE on the staging schema is the refusal no_grant (C8, seen on MariaDB)"""
    from sqlalchemy.exc import OperationalError
    from app.backend.services.backups.restore import RestoreRefused, _granted
    denied = OperationalError("DROP DATABASE", {}, Exception(1044, "Access denied"))
    with pytest.raises(RestoreRefused) as exc:
        with _granted("radio_monitoring_restore"):
            raise denied
    assert exc.value.code == "no_grant"
    other = OperationalError("DROP DATABASE", {}, Exception(2013, "Lost connection"))
    with pytest.raises(OperationalError):
        with _granted("radio_monitoring_restore"):
            raise other


@pytest.mark.parametrize("line", [
    "USE `radio_monitoring`;", "use radio_monitoring", "  USE x;", "CREATE DATABASE `x`;", "create schema y;",
    "DROP DATABASE radio_monitoring;", "ALTER DATABASE x CHARACTER SET utf8;", "\\u radio_monitoring",
    "\\! rm -rf /", "connect radio_monitoring", "system ls",
])
def test_unsafe_statements(line):
    assert unsafe_statement(f"-- dump\nCREATE TABLE `a` (id int);\n{line}\nINSERT INTO `a` VALUES (1);\n")


@pytest.mark.parametrize("text", [
    DUMP_BODY,
    "INSERT INTO `a` VALUES ('USE radio_monitoring;\\nDROP DATABASE x');\n",   # inside a value: escaped, one line
    "/*M!999999\\- enable the sandbox mode */ \n",
    "-- USE in a comment\n",
])
def test_safe_dumps(text):
    assert unsafe_statement(text) is None


@pytest.fixture
def backend(tmp_path):
    fakes = Fakes(tmp_path / "bin")
    fakes.client.configure(read_stdin=True)
    return fakes, MariaDbBackend(DB, fakes.tools, timeout_seconds=30)


def dump_file(tmp_path, text):
    path = tmp_path / "dump.sql.gz"
    path.write_bytes(gzip.compress(text.encode("utf-8")))
    return path


def test_load_runs_the_client_on_one_database_in_sandbox_mode(tmp_path, backend):
    fakes, real = backend
    real.load_dump(dump_file(tmp_path, DUMP_BODY), "radio_monitoring_restore")
    (record,) = fakes.client.records()
    assert record["argv"][-1] == "radio_monitoring_restore"
    assert "--one-database" in record["argv"] and "--sandbox" in record["argv"]
    assert record["stdin"].endswith("VALUES ('028_no_actor_foreign_keys');\n")


def test_load_refuses_a_dump_that_switches_database(tmp_path, backend):
    fakes, real = backend
    text = DUMP_BODY + "USE `radio_monitoring`;\nDROP TABLE IF EXISTS `users`;\n"
    with pytest.raises(BackupFailure) as exc:
        real.load_dump(dump_file(tmp_path, text), "radio_monitoring_restore")
    assert exc.value.code == "unsafe_dump" and exc.value.refused is True
    records = fakes.client.records()
    assert not records or "DROP TABLE IF EXISTS `users`" not in (records[0]["stdin"] or "")


@pytest.mark.parametrize("chunk", [7, 16, 31, 64])
def test_a_block_border_inside_a_line_is_no_statement(tmp_path, backend, monkeypatch, chunk):
    """Seen live: a block that starts inside a row (an escaped quote of a JSON value) is not a client command"""
    from app.backend.services.backups import restore
    monkeypatch.setattr(restore, "CHUNK", chunk)
    row = 'INSERT INTO `t` VALUES (1,\'{\\"page_size\\": 50, \\"sort\\": \\"asc\\"}\'),(2,\'USE x\');\n'
    fakes, real = backend
    real.load_dump(dump_file(tmp_path, DUMP_BODY + row * 20), "radio_monitoring_restore")
    (record,) = fakes.client.records()
    assert record["stdin"].endswith(row)


def test_an_unsafe_line_after_many_blocks_is_found(tmp_path, backend, monkeypatch):
    from app.backend.services.backups import restore
    monkeypatch.setattr(restore, "CHUNK", 16)
    with pytest.raises(BackupFailure) as exc:
        real = backend[1]
        real.load_dump(dump_file(tmp_path, DUMP_BODY * 3 + "USE `radio_monitoring`;\n"), "radio_monitoring_restore")
    assert exc.value.code == "unsafe_dump"


def test_an_unsafe_last_line_never_reaches_the_client(tmp_path, backend, monkeypatch):
    """The last line has no newline: it is checked before it is handed on"""
    from app.backend.services.backups import restore
    monkeypatch.setattr(restore, "CHUNK", 1024 * 1024)
    fakes, real = backend
    with pytest.raises(BackupFailure):
        real.load_dump(dump_file(tmp_path, DUMP_BODY + "\\! touch /tmp/pwned"), "radio_monitoring_restore")
    records = fakes.client.records()
    assert not records or "pwned" not in (records[0]["stdin"] or "")
