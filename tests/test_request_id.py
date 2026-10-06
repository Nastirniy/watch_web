"""Every response carries X-Request-ID: the id nginx gave the request ($request_id, sent in X-Request-ID), or one of
the API's own without nginx. The API log carries it on every line of the request and the error_id of a 500 is the
request id, so one id finds the request in the nginx access log and in the API log (tests/test_server_logging.py)."""
import re

import pytest

from app.backend.enums.routes import SystemRoutes

REQUEST_ID = re.compile(r"[0-9a-f]{32}")   # nginx's $request_id: 16 random bytes in hex


@pytest.mark.asyncio
async def test_every_response_carries_a_request_id(client):
    for path in (SystemRoutes.HEALTH.value, SystemRoutes.HEALTH.value + "-nowhere"):   # an answer and an error
        response = await client.get(path)
        assert REQUEST_ID.fullmatch(response.headers.get("x-request-id", "")), path


@pytest.mark.asyncio
async def test_two_requests_get_two_ids(client):
    first = await client.get(SystemRoutes.HEALTH.value)
    second = await client.get(SystemRoutes.HEALTH.value)
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.asyncio
async def test_the_request_id_nginx_gave_is_kept(client):
    given = "0123456789abcdef0123456789abcdef"
    response = await client.get(SystemRoutes.HEALTH.value, headers={"X-Request-ID": given})
    assert response.headers["x-request-id"] == given


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["short", 'quote"0123456789abcdef0123456789ab', "0123456789ABCDEF0123456789ABCDEF",
                                  "0123456789abcdef0123456789abcdef0", "x" * 300])
async def test_a_request_id_not_in_the_form_nginx_gives_is_replaced(client, sent):
    """Without nginx a client could send anything; it never reaches the log or the response as is"""
    response = await client.get(SystemRoutes.HEALTH.value, headers={"X-Request-ID": sent})
    returned = response.headers["x-request-id"]
    assert REQUEST_ID.fullmatch(returned) and returned not in (sent, sent.lower())
