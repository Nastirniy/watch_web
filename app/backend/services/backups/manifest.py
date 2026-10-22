"""The manifest of a backup folder: written last, never changed; a folder without a valid one is no backup"""
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.backend.enums.general import BackupLabel

FORMAT_VERSION = 1
# Modes: the backup user writes, the api reads folders and manifests through the backup group, a dump (password and
# token hashes) is readable by the backup user only
DIR_MODE = 0o750
SHARED_MODE = 0o640
PRIVATE_MODE = 0o600
MANIFEST_FILE = "manifest.json"
DUMP_FILE = "dump.sql.gz"


class ManifestError(Exception):
    """The manifest of a folder is missing or unusable; reason is short text for the log and the CLI"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Manifest:
    format_version: int
    id: str
    label: BackupLabel
    started_at: datetime
    finished_at: datetime
    database: str
    server_product: str
    server_version: str
    dump_tool: str
    migration_revision: Optional[str]
    tables: tuple[str, ...]
    file: str
    size_bytes: int
    sha256: str


def backup_id(started_at: datetime, label: BackupLabel) -> str:
    """UTC start time and label: 20261022T031500Z-scheduled (sorts by time)"""
    return f"{started_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{BackupLabel(label).value}"


def _to_json(manifest: Manifest) -> dict:
    data = asdict(manifest)
    data["label"] = manifest.label.value
    data["started_at"] = manifest.started_at.astimezone(timezone.utc).isoformat()
    data["finished_at"] = manifest.finished_at.astimezone(timezone.utc).isoformat()
    data["tables"] = list(manifest.tables)
    return data


def write_manifest(folder: Path, manifest: Manifest) -> None:
    """Write folder/manifest.json through a .tmp file and os.replace (0640: the api reads it through the backup
    group)"""
    target = folder / MANIFEST_FILE
    tmp = folder / (MANIFEST_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SHARED_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_to_json(manifest), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, SHARED_MODE)
    os.replace(tmp, target)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive datetime")
    return parsed.astimezone(timezone.utc)


def read_manifest(folder: Path) -> Manifest:
    """The manifest of folder; ManifestError when it is missing, not JSON, incomplete, of another format or of
    another folder"""
    path = folder / MANIFEST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError("no manifest")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest not readable ({type(exc).__name__})")
    except json.JSONDecodeError:
        raise ManifestError("manifest is not JSON")
    if not isinstance(data, dict):
        raise ManifestError("manifest is not an object")
    if data.get("format_version") != FORMAT_VERSION:
        raise ManifestError(f"unknown manifest format {data.get('format_version')!r}")
    try:
        manifest = Manifest(
            format_version=data["format_version"], id=str(data["id"]), label=BackupLabel(data["label"]),
            started_at=_utc(data["started_at"]), finished_at=_utc(data["finished_at"]),
            database=str(data["database"]), server_product=str(data["server_product"]),
            server_version=str(data["server_version"]), dump_tool=str(data["dump_tool"]),
            migration_revision=data["migration_revision"], tables=tuple(str(t) for t in data["tables"]),
            file=str(data["file"]), size_bytes=int(data["size_bytes"]), sha256=str(data["sha256"]),
        )
    except KeyError as exc:
        raise ManifestError(f"manifest misses {exc.args[0]}")
    except (TypeError, ValueError):
        raise ManifestError("manifest has a bad value")
    if manifest.id != folder.name:
        raise ManifestError("manifest of another folder")
    return manifest
