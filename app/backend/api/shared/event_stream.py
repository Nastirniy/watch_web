"""The one response class of the stream of events (text/event-stream). Version-neutral: the messages are the
version's (api/vN/models/response/events.py), the bytes come from the hub (services/events/hub.py)."""
from starlette.responses import StreamingResponse

from app.backend.enums.media_types import ContentType


class EventStreamResponse(StreamingResponse):
    """text/event-stream. A Response subclass: FastAPI serialises anything else. A stock StreamingResponse holds up
    under slow readers, drain and disconnects (measured), and costs 5 KB per stream over a class of our own.
    no-store: what EventSource itself sends; X-Accel-Buffering: no, so nginx passes each block on as it is written."""
    media_type = ContentType.EVENT_STREAM.value

    def __init__(self, content, status_code: int = 200, **kwargs):
        # status_code in the signature: FastAPI reads the default status of a response class from it
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        super().__init__(content, status_code=status_code, headers=headers, **kwargs)
