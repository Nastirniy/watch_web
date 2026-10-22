"""The schemas a restore uses and the MariaDB / MySQL statements around them.

<db>_restore holds a backup while it is loaded and checked, <db>_verify a deep verify, <db>_before_<ts> the tables a
restore (or an undo) replaced, kept for undo-restore until prune drops them (backup.keep_previous_days). Only these
three kinds are ever dropped.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

import sqlalchemy as sa

KEPT_FORMAT = "%Y%m%d_%H%M%S"


@dataclass(frozen=True)
class KeptSchema:
    name: str
    created_at: datetime
    size_bytes: int


def staging_name(db: str) -> str:
    return f"{db}_restore"


def verify_name(db: str) -> str:
    return f"{db}_verify"


def kept_name(db: str, now: datetime) -> str:
    return f"{db}_before_{now.astimezone(timezone.utc):{KEPT_FORMAT}}"


def kept_re(db: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(db)}_before_(\d{{8}}_\d{{6}})$")


def is_droppable(db: str, schema: str) -> bool:
    """Only the staging, verify and kept schemas of db may be dropped, never db itself"""
    return schema in (staging_name(db), verify_name(db)) or bool(kept_re(db).match(schema))


def quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def rename_statement(db: str, live: Sequence[str], incoming: Sequence[str], source: str, keep_as: str) -> str:
    """One RENAME TABLE (atomic): every live table to keep_as, then every table of source into db"""
    moves = [f"{quote(db)}.{quote(t)} TO {quote(keep_as)}.{quote(t)}" for t in live]
    moves += [f"{quote(source)}.{quote(t)} TO {quote(db)}.{quote(t)}" for t in incoming]
    return "RENAME TABLE " + ", ".join(moves)


UNSUPPORTED_OBJECTS = sa.text("""
    SELECT CONCAT('view ', TABLE_NAME) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = :schema
    UNION ALL SELECT CONCAT('trigger ', TRIGGER_NAME) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = :schema
    UNION ALL SELECT CONCAT(LOWER(ROUTINE_TYPE), ' ', ROUTINE_NAME) FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA = :schema
    UNION ALL SELECT CONCAT('event ', EVENT_NAME) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = :schema
    UNION ALL SELECT CONCAT(LOWER(TABLE_TYPE), ' ', TABLE_NAME, ' (', COALESCE(ENGINE, '-'), ')')
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE <> 'VIEW'
          AND NOT (TABLE_TYPE = 'BASE TABLE' AND ENGINE = 'InnoDB')
""")

# Connections of other clients using the schema. A user sees its own threads without PROCESS: the api and the
# worker connect as the same user as this tool, so they are always counted.
OTHER_CONNECTIONS = sa.text(
    "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE DB = :schema AND ID <> CONNECTION_ID()")

LIST_TABLES = sa.text(
    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = :schema AND TABLE_TYPE = 'BASE TABLE' "
    "ORDER BY TABLE_NAME")

SCHEMA_CHARSET = sa.text(
    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
    "WHERE SCHEMA_NAME = :schema")


def kept_schemas_query() -> sa.TextClause:
    """Schemas named like the kept ones of a database with their size; bind :pattern with kept_pattern(db)"""
    return sa.text(
        "SELECT s.SCHEMA_NAME, COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH), 0) FROM information_schema.SCHEMATA s "
        "LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME "
        "WHERE s.SCHEMA_NAME LIKE :pattern GROUP BY s.SCHEMA_NAME")


def kept_pattern(db: str) -> str:
    escaped = db.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
    return f"{escaped}\\_before\\_%"


def parse_kept(db: str, rows: Iterable[tuple]) -> list[KeptSchema]:
    """KeptSchema of every row (name, size) that is a kept schema of db, oldest first"""
    pattern = kept_re(db)
    kept = []
    for name, size in rows:
        match = pattern.match(name)
        if match:
            created = datetime.strptime(match.group(1), KEPT_FORMAT).replace(tzinfo=timezone.utc)
            kept.append(KeptSchema(name=name, created_at=created, size_bytes=int(size or 0)))
    return sorted(kept, key=lambda k: k.created_at)
