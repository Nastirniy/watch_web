"""The ring: replay from memory after a reconnect, never from the database"""
from datetime import datetime

import pytest

from app.backend.services.events.reader import OutboxRow
from app.backend.services.events.ring import Ring, block, cursor_token, parse_cursor, parse_position


def row(row_id: int, scope_key: str = "root") -> OutboxRow:
    return OutboxRow(id=row_id, type="scope.changed", v=1, scope_key=scope_key, permission="post.view", user_id=None,
                     conflate_key=None, payload="{}", occurred_at=datetime(2026, 11, 22), lag_ms=0)


def body(row_id: int) -> bytes:
    return b"event: scope.changed\ndata: {\"id\":\"%d\"}\n\n" % row_id


def ring_of(*ids: int, capacity: int = 100, floor: int = 0) -> Ring:
    ring = Ring(capacity, floor=floor)
    for row_id in ids:
        ring.add(row(row_id), body(row_id))
    return ring


def ids(frames) -> list:
    return [frame_row.id for frame_row, _ in frames]


def test_a_position_inside_the_ring_gives_the_rows_after_it_in_order():
    ring = ring_of(1, 2, 3, 4)
    assert ids(ring.after(2, head=4, limit=10)) == [3, 4]
    assert ids(ring.after(4, head=4, limit=10)) == []
    assert ids(ring.after(0, head=4, limit=10)) == [1, 2, 3, 4]


def test_a_position_older_than_the_ring_is_none():
    ring = ring_of(5, 6, floor=4)
    assert ring.after(3, head=6, limit=10) is None
    assert ids(ring.after(4, head=6, limit=10)) == [5, 6]


def test_a_position_ahead_of_the_head_is_none():
    ring = ring_of(1, 2)
    assert ring.after(3, head=2, limit=10) is None


@pytest.mark.parametrize("value", [None, "", "abc", "-1", "1.5", " 7", "7 ", "\u0661", "1" * 21, "0x10"])
def test_a_garbage_position_is_none(value):
    assert parse_position(value) is None


def test_a_position_of_ours_parses():
    assert parse_position("0") == 0 and parse_position("123456789") == 123456789


def test_more_matching_rows_than_the_limit_is_none():
    ring = ring_of(*range(1, 11))
    assert ring.after(0, head=10, limit=9) is None
    assert ids(ring.after(0, head=10, limit=10)) == list(range(1, 11))
    # only the rows a connection may receive count
    assert ids(ring.after(0, head=10, limit=5, match=lambda frame_row: frame_row.id % 2 == 0)) == [2, 4, 6, 8, 10]


def test_a_late_row_lands_in_order():
    ring = ring_of(1, 3, 4)
    ring.add(row(2), body(2))
    assert ids(ring.after(0, head=4, limit=10)) == [1, 2, 3, 4]


def test_the_ring_never_exceeds_its_capacity_and_its_floor_rises():
    ring = ring_of(*range(1, 8), capacity=5)
    assert len(ring) == 5
    assert ids(ring.after(2, head=7, limit=10)) == [3, 4, 5, 6, 7]
    assert ring.after(1, head=7, limit=10) is None, "row 2 left the ring: a client at 1 would miss it"


def test_the_frames_of_a_replay_carry_the_current_position():
    ring = ring_of(1, 2, 3)
    frames = ring.after(1, head=3, limit=10)
    blocks = [block(3, frame_body) for _, frame_body in frames]
    assert blocks[0].startswith(b"id: 3\nevent: scope.changed\n") and all(b.startswith(b"id: 3\n") for b in blocks)


# ------------------------------------------------------------------ cursors with late ids

def test_a_cursor_without_late_ids_is_the_plain_position():
    assert cursor_token(42, []) == "42"
    assert parse_cursor("42") == (42, frozenset())


def test_late_ids_travel_as_distances_below_the_position():
    token = cursor_token(1000, [990, 997])
    assert token == "1000.10.3"
    assert parse_cursor(token) == (1000, frozenset({990, 997}))
    assert parse_position(token) == 1000


@pytest.mark.parametrize("value", ["1000.", "1000.0", "5.6", "1000.x", "." , "1000." + ".".join(["1"] * 40),
                                   "1000.-3", "1000.1.1e3"])
def test_a_cursor_that_is_not_ours_is_none(value):
    assert parse_cursor(value) is None and parse_position(value) is None


def test_a_late_row_that_arrived_is_replayed_with_the_rows_after_the_position():
    ring = ring_of(1, 3, 4)
    ring.add(row(2), body(2))                       # committed late, after the client got position 3
    assert ids(ring.after(3, head=4, limit=10, late=frozenset({2}))) == [2, 4]
    assert ids(ring.after(3, head=4, limit=10)) == [4]


def test_a_late_id_that_never_arrived_replays_nothing_for_it():
    ring = ring_of(1, 3, 4)
    assert ids(ring.after(3, head=4, limit=10, late=frozenset({2}))) == [4]


def test_a_late_id_at_or_below_the_floor_cannot_be_answered_for():
    ring = ring_of(5, 6, floor=4)
    assert ring.after(6, head=6, limit=10, late=frozenset({4})) is None

