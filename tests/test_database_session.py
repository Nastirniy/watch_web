"""The request's database session: it rolls back on any error and leaves the logging to the error path."""
import pytest
from loguru import logger

from app.backend import database
from app.backend.enums.errors import ErrorCode
from app.backend.services.api_errors import ApiError


class FakeSession:
    def __init__(self):
        self.rolled_back = False

    async def rollback(self):
        self.rolled_back = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.parametrize("error", [ApiError(ErrorCode.MEDIA_NOT_FOUND), RuntimeError("boom")], ids=["api_error", "unhandled"])
@pytest.mark.asyncio
async def test_an_error_in_the_request_is_rolled_back_and_not_logged_by_the_session(monkeypatch, error):
    """A 4xx is the answer, not an incident; a 500 is logged once with its error_id by UnhandledErrorMiddleware"""
    session = FakeSession()

    async def session_maker():
        return lambda: session

    monkeypatch.setattr(database, "get_session_maker", session_maker)
    records = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="WARNING")
    try:
        dependency = database.get_session()
        assert await dependency.__anext__() is session
        with pytest.raises(type(error)):
            await dependency.athrow(error)
    finally:
        logger.remove(sink)

    assert session.rolled_back
    assert records == []
