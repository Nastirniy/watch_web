"""The ring: the last rows of a worker, as the bytes of their message, sorted by id. Replay comes only from here, so a
reconnect costs no event query and a reconnect storm has no replay cost. A position older than the ring, ahead of the
head, or a replay above the limit is reload (the hub decides). The limit stays below the queue of a connection: a
replay through a queue it overflows would close the stream, and the client would reconnect and replay for ever.

A frame is (row, body): body is the bytes from "event:" on. The "id:" line is put in front when it is sent, with the
cursor of that moment: a replayed block never carries the stamp it was built with, or the client's cursor would
move back.

The cursor is the position plus the late ids: ids below the position not committed yet when it was given (the position
passes a gap after position_hold_seconds, gaps.py). Written "<position>.<d1>.<d2>", each d the distance of a late id
below the position. A replay gives the rows after the position and every late id that arrived meanwhile, so a client
that reconnects after a late commit still gets its row (plan test 6, measured on MariaDB 2026-11-23: a position alone
lost the row for 49 of 60 reconnected streams). Opaque to the client (API.md), so the format may change.
"""
from bisect import bisect_left, bisect_right
from typing import Callable, FrozenSet, List, NamedTuple, Optional, Sequence, Tuple, Union

from app.backend.services.events.reader import OutboxRow

Frame = Tuple[OutboxRow, bytes]


MAX_LATE = 16     # late ids a cursor names; with more, the position stays below the lowest (gaps.cursor)


class Cursor(NamedTuple):
    position: int
    late: FrozenSet[int]


def cursor_token(position: int, late: Sequence[int]) -> str:
    """The cursor as sent in "id:": the position, then the distance of each late id below it"""
    return ".".join([str(position), *(str(position - row_id) for row_id in late)])


def parse_cursor(value: Optional[str]) -> Optional[Cursor]:
    """The cursor a client sent, None when it is not one of ours. Never an error: the format may change"""
    if value is None or not value.isascii():
        return None
    parts = value.split(".")
    if len(parts) > 1 + MAX_LATE or not all(part.isdigit() and len(part) <= 20 for part in parts):
        return None
    position = int(parts[0])
    distances = [int(part) for part in parts[1:]]
    if any(distance < 1 or distance >= position for distance in distances):
        return None
    return Cursor(position, frozenset(position - distance for distance in distances))


def parse_position(value: Optional[str]) -> Optional[int]:
    """The position of the cursor a client sent, None when it is not one of ours"""
    cursor = parse_cursor(value)
    return None if cursor is None else cursor.position


def block(cursor: Union[int, str], body: bytes) -> bytes:
    """A message as sent: the cursor, then the body"""
    return b"id: %s\n" % str(cursor).encode("ascii") + body


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
              match: Callable[[OutboxRow], bool] = lambda row: True,
              late: FrozenSet[int] = frozenset()) -> Optional[List[Frame]]:
        """The frames of the late ids that arrived, then those after position, that match, in id order; None when the
        ring cannot answer (position older than the ring or ahead of the head, a late id at or below the floor: it may
        have arrived and gone) or more than limit match"""
        if position < self.floor or position > head or (late and min(late) <= self.floor):
            return None
        frames: List[Frame] = []
        for row_id in sorted(late):
            index = bisect_left(self._ids, row_id)
            if index < len(self._ids) and self._ids[index] == row_id and match(self._frames[index][0]):
                frames.append(self._frames[index])
        frames += [frame for frame in self._frames[bisect_right(self._ids, position):] if match(frame[0])]
        return frames if len(frames) <= limit else None


__all__ = ["Cursor", "Frame", "MAX_LATE", "Ring", "block", "cursor_token", "parse_cursor", "parse_position"]
