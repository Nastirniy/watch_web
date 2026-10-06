"""One id per request, from the nginx access log to the API log: X-Request-ID, the de facto standard header.

Behind nginx the id is nginx's $request_id (nginx-proxy.conf sets X-Request-ID and replaces whatever the client
sent), so both logs share it; without nginx the API makes one in the same form. Every response carries it, every
API log line of the request carries it (utils/logging_config.py) and the error_id of a 500 is the request id: one id
for a user to quote, whichever layer answered (API.md)."""
import re
import uuid
from typing import Optional

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.backend.utils.logging_config import request_id_context

REQUEST_ID_HEADER = "X-Request-ID"
# nginx's $request_id: 16 random bytes in hex. Anything else (a client without nginx) is replaced, never logged.
REQUEST_ID_FORM = re.compile(r"[0-9a-f]{32}")


def current_request_id() -> Optional[str]:
    return request_id_context.get()


class RequestIdMiddleware:
    """The outermost middleware: the id is set before anything can log or fail, and answered on every response"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sent = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = sent if sent and REQUEST_ID_FORM.fullmatch(sent) else uuid.uuid4().hex

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        token = request_id_context.set(request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_context.reset(token)
