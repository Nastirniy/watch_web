"""Events to the web client (API.md "Real-time events"). No FastAPI here: the response class is in
api/shared/event_stream.py and the renderer of a version comes from app/main.py.

publish.py   the one writer of outbox_events, in the transaction of the change
bus.py       the seam for a broker (EventBus, OutboxBus)
gaps.py      late commits: the position stays below an id not yet visible
reader.py    the reads of a tick, on the hub's own engine
ring.py      the last rows of each worker, replay from memory
rights.py    who receives an event: an index over the same grants and tree as a request
connection.py, hub.py, session_check.py   the streams of one worker
cleanup.py   rows past retention, removed by the media worker
"""
from app.backend.services.events.publish import publish, publish_scope_changed

__all__ = ["publish", "publish_scope_changed"]
