"""The state of the database server and of the schema (GET /database/status). Read-only.

MariaDB / MySQL: information_schema.TABLES of the current database (estimated rows and sizes) and a few named rows
of SHOW GLOBAL STATUS / VARIABLES. SQLite (tests, development) has no such figures: they are None.
"""
from dataclasses import dataclass
from typing import Optional

import sqlalchemy as sa
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.utils.alembic_history import code_head

TABLES = sa.text(
    "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")
STATUS = sa.text(
    "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Threads_connected', 'Max_used_connections')")
MAX_CONNECTIONS = sa.text("SHOW GLOBAL VARIABLES LIKE 'max_connections'")


@dataclass(frozen=True)
class ServerInfo:
    product: str                     # mariadb, mysql or sqlite
    version: str
    uptime_seconds: Optional[int]
    threads_connected: Optional[int]
    max_used_connections: Optional[int]
    max_connections: Optional[int]


@dataclass(frozen=True)
class TableInfo:
    name: str
    rows_estimate: Optional[int]
    data_bytes: Optional[int]
    index_bytes: Optional[int]


@dataclass(frozen=True)
class SchemaInfo:
    name: str
    migration_revision: Optional[str]
    code_revision: str
    up_to_date: bool
    size_bytes: Optional[int]
    tables: tuple[TableInfo, ...]     # largest first


@dataclass(frozen=True)
class DatabaseStatus:
    server: ServerInfo
    schema: SchemaInfo


def _int(value) -> Optional[int]:
    return None if value is None else int(value)


async def _revision(conn, table_names) -> Optional[str]:
    if "alembic_version" not in table_names:
        return None
    return (await conn.execute(sa.text("SELECT version_num FROM alembic_version"))).scalar()


async def database_status(session: AsyncSession) -> DatabaseStatus:
    conn = await session.connection()
    head = code_head()
    if conn.dialect.name == "sqlite":
        version = (await conn.execute(sa.text("SELECT sqlite_version()"))).scalar()
        names = sorted(await conn.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
        revision = await _revision(conn, names)
        return DatabaseStatus(
            server=ServerInfo(product="sqlite", version=version, uptime_seconds=None, threads_connected=None,
                              max_used_connections=None, max_connections=None),
            schema=SchemaInfo(name="main", migration_revision=revision, code_revision=head,
                              up_to_date=revision == head, size_bytes=None,
                              tables=tuple(TableInfo(name=n, rows_estimate=None, data_bytes=None, index_bytes=None)
                                           for n in names)))

    version = (await conn.execute(sa.text("SELECT VERSION()"))).scalar()
    name = (await conn.execute(sa.text("SELECT DATABASE()"))).scalar()
    status = {row[0].lower(): row[1] for row in (await conn.execute(STATUS)).all()}
    max_connections = (await conn.execute(MAX_CONNECTIONS)).first()
    tables = sorted((TableInfo(name=row[0], rows_estimate=_int(row[1]), data_bytes=_int(row[2]),
                               index_bytes=_int(row[3])) for row in (await conn.execute(TABLES)).all()),
                    key=lambda t: (t.data_bytes or 0) + (t.index_bytes or 0), reverse=True)
    revision = await _revision(conn, {t.name for t in tables})
    return DatabaseStatus(
        server=ServerInfo(
            product="mariadb" if "mariadb" in version.lower() else "mysql", version=version,
            uptime_seconds=_int(status.get("uptime")), threads_connected=_int(status.get("threads_connected")),
            max_used_connections=_int(status.get("max_used_connections")),
            max_connections=None if max_connections is None else _int(max_connections[1])),
        schema=SchemaInfo(name=name, migration_revision=revision, code_revision=head, up_to_date=revision == head,
                          size_bytes=sum((t.data_bytes or 0) + (t.index_bytes or 0) for t in tables),
                          tables=tuple(tables)))
