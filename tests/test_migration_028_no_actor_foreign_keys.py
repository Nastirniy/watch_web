"""028_no_actor_foreign_keys: the actor columns (audit_logs.user_id, user_certificates.created_by / revoked_by) have
no foreign key to users, the owner column user_certificates.user_id keeps its."""
import sqlite3
from pathlib import Path

from app.backend.models.audit import AuditLog
from app.backend.models.user_certificate import UserCertificate
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, run_migrate

REVISION = "028_no_actor_foreign_keys"
PREVIOUS = "027_scope_group_kinds"


def _fk_columns(db_file: Path, table: str) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[3] for row in conn.execute(f"pragma foreign_key_list({table})") if row[2] == "users"}
    conn.close()
    return names


def test_models_have_no_foreign_key_on_actor_columns():
    assert not AuditLog.__table__.c.user_id.foreign_keys
    assert not UserCertificate.__table__.c.created_by.foreign_keys
    assert not UserCertificate.__table__.c.revoked_by.foreign_keys
    assert UserCertificate.__table__.c.user_id.foreign_keys


def test_up_down_up_drops_and_restores_the_foreign_keys(tmp_path):
    db_file = tmp_path / "actors.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _fk_columns(db_file, "audit_logs") == set()
    assert _fk_columns(db_file, "user_certificates") == {"user_id"}

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert _fk_columns(db_file, "audit_logs") == {"user_id"}
    assert _fk_columns(db_file, "user_certificates") == {"user_id", "created_by", "revoked_by"}

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _fk_columns(db_file, "audit_logs") == set()
    assert _fk_columns(db_file, "user_certificates") == {"user_id"}
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()
