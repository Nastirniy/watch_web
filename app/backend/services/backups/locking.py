"""One writer at a time: every command that writes the backup directory or restores holds .state/lock"""
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.backend.services.backups.catalog import STATE_DIR
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.manifest import DIR_MODE, PRIVATE_MODE

LOCK_FILE = "lock"

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def writer_lock(directory: Path) -> Iterator[None]:
    """Hold the lock of directory; BackupFailure("busy") at once when another command holds it"""
    state = directory / STATE_DIR
    state.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    fd = os.open(state / LOCK_FILE, os.O_RDWR | os.O_CREAT, PRIVATE_MODE)
    try:
        if not _try_lock(fd):
            raise BackupFailure("busy")
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
