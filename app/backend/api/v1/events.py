"""GET /events/stream: the stream of events to the web client (API.md, "Real-time events")"""
from random import randint
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.event_stream import EventStreamResponse
from app.backend.api.shared.openapi_docs import empty_response
from app.backend.api.v1.models.response.events import StreamMessage
from app.backend.auth.context import AuthContext
from app.backend.auth.dependencies import TokenClaims, get_token_claims
from app.backend.auth.require import require_stream
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import EventRoutes
from app.backend.errors.openapi import problems
from app.backend.errors.problems import ApiError

router = APIRouter(prefix=EventRoutes.BASE.value, tags=[EventRoutes.tag()])


@router.get(
    EventRoutes.STREAM.value,
    response_class=EventStreamResponse,
    responses={
        200: {"model": StreamMessage,
              "description": "One text/event-stream message per item: `event:` is the `type`, `data:` this JSON, "
                             "`id:` the position to send back as Last-Event-ID"},
        **empty_response(204, "Events are off on this server: do not reconnect"),
        **problems(ErrorCode.EVENT_STREAM_CAPACITY),
    },
)
async def stream_events(
        request: Request,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require_stream()),
        claims: TokenClaims = Depends(get_token_claims),
        last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
):
    """The stream of events: a hint that something on screen changed, never data and never history. Everything works
    without it; refetch through the REST routes when an event says so. Any authenticated user; one stream per tab,
    read with fetch (Authorization: Bearer, Accept: text/event-stream), only over HTTP/2 (API.md).

    Messages (StreamMessage, discriminated by type): ready first; heartbeat about every 20 s; reload when the
    server cannot replay from the position sent (refetch everything on screen); close with a reason before the
    server ends the stream; domain events (move.changed, scope.changed, security.certificate_mismatch, and for the
    administration user.changed, certificate.changed, session.changed, role.changed, dictionary.changed). A domain
    event carries refetch_within_ms: refetch after a random wait below it, never at once.

    Two ids, both opaque. The `id:` line of every block is a position: store it (heartbeats included) and send it
    back as Last-Event-ID when reconnecting; never compare or compute it. A position this server cannot answer for
    (unknown, malformed, too old, from before a restore) gives reload, never an error. The `id` in the JSON is the
    identity of an event: ignore one already seen (delivery is at least once). Order holds within one resource only.

    Opening a stream and keeping it open is not activity: the session is not touched, so an idle tab still reaches
    the idle timeout. The stream closes with token_expired at the expiry of the access token (refresh with
    X-Activity: background, then reconnect), with session_ended within 10 s of a logout, an idle end, a revocation or
    an account change (stop), and with restart when the server stops (reconnect after its retry). A right lost
    stops the events of that place within 30 s.

    204: events are off on this server, do not reconnect until the page reloads. 503 event_stream_capacity with
    Retry-After: this server holds as many streams as it can, it is stopping, or its event hub is not running yet
    (it is retried); back off for Retry-After at least. 429, 502, 503 and 504
    without a problem body come from nginx (too many streams of one certificate, the API restarting): back off by
    status."""
    if not (settings.events.enabled and settings.events.serve_streams):
        return Response(status_code=204)
    hub = getattr(request.app.state, "event_hub", None)
    if hub is None:
        # This process should serve streams, but its hub has not started (app/main.py retries the start): a client
        # that stopped on 204 would never come back, so back off for longer than when the hub is full
        raise ApiError(ErrorCode.EVENT_STREAM_CAPACITY, headers={"Retry-After": str(randint(30, 120))})
    connection = hub.attach(auth, claims.expires_at, last_event_id)
    if connection is None:
        raise ApiError(ErrorCode.EVENT_STREAM_CAPACITY, headers={"Retry-After": str(randint(5, 30))})
    # The stream never holds a database connection. FastAPI 0.116 ends the dependency before the response; 0.118 and
    # later end it after, which would hold a pooled connection per stream. Explicit, whatever the version
    await session.close()
    return EventStreamResponse(connection.frames())
