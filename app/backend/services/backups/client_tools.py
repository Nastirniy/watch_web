"""The database client binaries and their credentials file.

mariadb-dump / mariadb (MariaDB), else mysqldump / mysql (MySQL 8). The password reaches them only through a
temporary --defaults-extra-file (0600 in a 0700 directory, removed afterwards), never through argv or the
environment, where ps or /proc would show it.
"""
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.backend.config import DatabaseConfig
from app.backend.services.backups.failures import BackupFailure


@dataclass(frozen=True)
class ClientTools:
    dump: str        # mariadb-dump or mysqldump
    client: str      # mariadb or mysql
    product: str     # mariadb or mysql (the binaries, not the server)


def find_tools() -> ClientTools:
    for dump, client, product in (("mariadb-dump", "mariadb", "mariadb"), ("mysqldump", "mysql", "mysql")):
        dump_path, client_path = shutil.which(dump), shutil.which(client)
        if dump_path and client_path:
            return ClientTools(dump=dump_path, client=client_path, product=product)
    raise BackupFailure("tools_missing")


def _quoted(value: str) -> str:
    """An option file value: double quotes, backslash and quote escaped"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def defaults_file(db: DatabaseConfig) -> Iterator[Path]:
    """A [client] option file with the credentials of db, removed when the block ends"""
    directory = Path(tempfile.mkdtemp(prefix="watch-web-db-"))
    try:
        os.chmod(directory, 0o700)
        path = directory / "client.cnf"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("[client]\n")
            f.write(f"user={_quoted(db.user)}\n")
            f.write(f"password={_quoted(db.password)}\n")
            f.write(f"host={_quoted(db.host)}\n")
            f.write(f"port={int(db.port)}\n")
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)
