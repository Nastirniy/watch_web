"""Known constraints are recognised by the driver's error, never by the SQL text or its parameters."""
import sqlite3

from sqlalchemy.exc import DataError, IntegrityError

from app.backend.api.shared.helpers.db_errors import is_data_too_long, is_unique_violation


class MySQLError(Exception):
    """Shape of pymysql.err.* (what aiomysql raises): args = (errno, message)"""


def wrap(error_class, orig, params=("x",)):
    return error_class("INSERT INTO posts (code) VALUES (%s)", params, orig)


def test_unique_violation_by_driver_error():
    assert is_unique_violation(wrap(IntegrityError, MySQLError(1062, "Duplicate entry 'A' for key 'posts.ix_posts_code'")))
    assert is_unique_violation(wrap(IntegrityError, MySQLError(1062, "localized message, no english text")))
    assert is_unique_violation(wrap(IntegrityError, sqlite3.IntegrityError("UNIQUE constraint failed: posts.code")))


def test_other_constraints_are_not_unique_violations():
    assert not is_unique_violation(wrap(IntegrityError, MySQLError(1452, "Cannot add or update a child row: a foreign key constraint fails")))
    assert not is_unique_violation(wrap(IntegrityError, sqlite3.IntegrityError("FOREIGN KEY constraint failed")))
    # the parameters are part of str(exc), never of the decision
    assert not is_unique_violation(wrap(IntegrityError, MySQLError(1048, "Column 'name' cannot be null"), params=("Duplicate entry",)))


def test_data_too_long():
    assert is_data_too_long(wrap(DataError, MySQLError(1406, "Data too long for column 'username' at row 1")))
    assert not is_data_too_long(wrap(DataError, MySQLError(1366, "Incorrect integer value")))
    assert not is_data_too_long(wrap(IntegrityError, sqlite3.IntegrityError("UNIQUE constraint failed: users.username")))
