"""One stream: a bounded queue of blocks and the body of the response.

Nothing grows. A queue above queue_max means a client that stopped reading: the stream ends without a last block
(that client reads nothing anyway) and the client reconnects with its position. No write timeout of our own: a
blocked write ends when nginx's send_timeout gives up (proved with slow readers). A block with a conflate key
replaces the queued block of the same key: only the latest waits.
"""
import asyncio
import time
from collections import deque
from typing import AsyncIterator, Callable, Deque, Optional, Tuple

from app.backend.enums.events import StreamCloseReason


class Connection:
    __slots__ = ("user_id", "session_id", "expires_at", "queue_max", "queue", "wake", "closing", "close_retry_ms",
                 "slow", "next_beat", "opened_at", "on_end", "ended", "closed_at")

    def __init__(self, user_id: int, session_id: Optional[str], expires_at: float, queue_max: int,
                 next_beat: float = 0.0, opened_at: float = 0.0):
        self.user_id = user_id
        self.session_id = session_id
        self.expires_at = expires_at            # exp of the access token (POSIX seconds)
        self.queue_max = queue_max
        self.queue: Deque[Tuple[bytes, Optional[str]]] = deque()
        self.wake = asyncio.Event()
        self.closing: Optional[StreamCloseReason] = None
        self.close_retry_ms: Optional[int] = None
        self.slow = False
        self.next_beat = next_beat              # monotonic time of the next heartbeat
        self.opened_at = opened_at
        self.on_end: Optional[Callable[["Connection"], None]] = None
        self.ended = False
        self.closed_at: Optional[float] = None    # monotonic: when it was closed or found slow

    @property
    def open(self) -> bool:
        return self.closing is None and not self.slow and not self.ended

    def offer(self, block: bytes, key: Optional[str] = None) -> None:
        """Queue a block; conflated by key; above queue_max the stream is slow and ends"""
        if not self.open:
            return
        if key is not None:
            for index, (_, queued_key) in enumerate(self.queue):
                if queued_key == key:
                    del self.queue[index]
                    break
        self.queue.append((block, key))
        if len(self.queue) > self.queue_max:
            self.slow = True
            self.closed_at = time.monotonic()
            self.queue.clear()
        self.wake.set()

    def close(self, reason: StreamCloseReason, last_block: bytes, retry_ms: Optional[int] = None) -> None:
        """The last block goes after what is queued (it never counts against the queue), then the stream ends"""
        if not self.open:
            return
        self.queue.append((last_block, None))
        self.closing, self.close_retry_ms = reason, retry_ms
        self.closed_at = time.monotonic()
        self.wake.set()

    async def frames(self) -> AsyncIterator[bytes]:
        """The body of the stream: everything queued since the last write as one chunk (one write per tick is why a
        fan-out is cheap); ends after the close block, at once when slow. on_end runs however it ends (a client gone
        cancels the generator)."""
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                if self.slow:
                    return
                if self.queue:
                    chunk = b"".join(block for block, _ in self.queue)
                    self.queue.clear()
                    yield chunk
                if self.closing is not None and not self.queue:
                    return
        finally:
            self.ended = True
            if self.on_end is not None:
                self.on_end(self)
