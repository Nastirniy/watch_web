"""Classify database errors by the driver's own error, never by the SQL text or its parameters (str(exc) has both)."""
from sqlalchemy.exc import DBAPIError, IntegrityError

# MySQL error numbers: language independent, unlike the message (lc_messages)
MYSQL_DUPLICATE_ENTRY = 1062
MYSQL_DATA_TOO_LONG = 1406


def _mysql_errno(exc: DBAPIError) -> int | None:
    args = getattr(exc.orig, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def is_unique_violation(exc: IntegrityError) -> bool:
    """A unique index refused the row: MySQL 1062, sqlite "UNIQUE constraint failed" """
    return _mysql_errno(exc) == MYSQL_DUPLICATE_ENTRY or "UNIQUE constraint failed" in str(exc.orig)


def is_data_too_long(exc: DBAPIError) -> bool:
    """A value is longer than its column: MySQL 1406 (sqlite does not check lengths)"""
    return _mysql_errno(exc) == MYSQL_DATA_TOO_LONG
