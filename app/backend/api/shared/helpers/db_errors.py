"""unique_violation_as: a unique violation of a flush or commit answered as a problem. The classification is
utils/db_errors.py, which services read too."""
from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError

from app.backend.utils.db_errors import (  # noqa: F401  the routes import them from here
    is_data_too_long, is_unique_violation, is_write_conflict,
)


@asynccontextmanager
async def unique_violation_as(session, code, **params):
    """Run the flush / commit inside; a unique violation (two requests passed the same SELECT check) rolls the
    session back and answers `code`. Any other IntegrityError is not this failure and propagates (global 500)."""
    from app.backend.errors.problems import ApiError

    try:
        yield
    except IntegrityError as exc:
        await session.rollback()
        if not is_unique_violation(exc):
            raise
        raise ApiError(code, **params) from exc
