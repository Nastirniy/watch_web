"""The ring: the last rows of a worker, as the bytes of their message, sorted by id. Replay comes only from here, so a
reconnect costs no event query and a reconnect storm has no replay cost. A position older than the ring, ahead of the
head, or a replay above the limit is reload (the hub decides). The limit stays below the queue of a connection: a
replay through a queue it overflows would close the stream, and the client would reconnect and replay for ever.

A frame is (row, body): body is the bytes from "event:" on. The "id:" line is put in front when it is sent, with the
position of that moment: a replayed block never carries the stamp it was built with, or the client's cursor would
move back.
"""
from bisect import bisect_right
from typing import Callable, List, Optional, Tuple

from app.backend.services.events.reader import OutboxRow

Frame = Tuple[OutboxRow, bytes]


def block(position: int, body: bytes) -> bytes:
    """A message as sent: the position, then the body"""
    return b"id: %d\n" % position + body


def parse_position(value: Optional[str]) -> Optional[int]:
    """The position a client sent, None when it is not one of ours. Never an error: the format may change"""
    if value is None or not value.isascii() or not value.isdigit() or len(value) > 20:
        return None
    return int(value)


class Ring:
    def __init__(self, capacity: int, floor: int = 0):
        self.capacity = capacity
        self.floor = floor              # every row above it is here (or arrives late into its place)
        self._ids: List[int] = []
        self._frames: List[Frame] = []

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, row: OutboxRow, body: bytes) -> None:
        if row.id <= self.floor:
            return                      # older than what the ring holds: a client behind it gets reload anyway
        index = bisect_right(self._ids, row.id)
        if index and self._ids[index - 1] == row.id:
            return
        self._ids.insert(index, row.id)
        self._frames.insert(index, (row, body))
        while len(self._ids) > self.capacity:
            self.floor = max(self.floor, self._ids.pop(0))
            self._frames.pop(0)

    def after(self, position: int, head: int, limit: int,
              match: Callable[[OutboxRow], bool] = lambda row: True) -> Optional[List[Frame]]:
        """The frames after position that match, in id order; None when the ring cannot answer for position (older
        than the ring, ahead of the head) or more than limit match"""
        if position < self.floor or position > head:
            return None
        frames = [frame for frame in self._frames[bisect_right(self._ids, position):] if match(frame[0])]
        return frames if len(frames) <= limit else None


__all__ = ["Frame", "Ring", "block", "parse_position"]
