"""Backups at start-up: with backup.dir set, the directories the API reads must be readable (no FastAPI here)"""
import os

from app.backend.config import BackupConfig


class BackupSettingsError(RuntimeError):
    """settings.backup points at a directory the API cannot read; the API refuses to start"""


def _readable_directory(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def check_backup_config(cfg: BackupConfig) -> None:
    """Nothing set is fine; a set backup.dir / backup.copy_dir must be a readable directory (the mount is missing
    otherwise: check BACKUP_DIR / BACKUP_COPY_DIR and docker-compose.backup*.yml)"""
    if not cfg.is_configured():
        return
    if not _readable_directory(cfg.dir):
        raise BackupSettingsError("backup.dir is set but is not a readable directory (MYSETTINGS_BACKUP__DIR)")
    if cfg.copy_dir and not _readable_directory(cfg.copy_dir):
        raise BackupSettingsError(
            "backup.copy_dir is set but is not a readable directory (MYSETTINGS_BACKUP__COPY_DIR)")
