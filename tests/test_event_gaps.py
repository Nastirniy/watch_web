"""Late commits: the gap tracker. Pure logic, no database, no clock."""
from app.backend.services.events.gaps import GapTracker


def tracker(**overrides):
    return GapTracker(**{"ttl": 60, "hold": 8, "max_jump": 1000, "max_gaps": 5000, **overrides})


def test_no_gap_the_position_is_the_head():
    t = tracker()
    t.saw([1, 2, 3], now=0)
    assert (t.head, t.position(now=0), t.open_ids()) == (3, 3, [])


def test_a_skipped_id_holds_the_position_below_it():
    t = tracker()
    t.saw([1, 3], now=0)
    assert t.open_ids() == [2]
    assert t.position(now=0) == 1


def test_a_late_row_closes_the_gap():
    t = tracker()
    t.saw([1, 3], now=0)
    t.saw([2], now=1)
    assert t.open_ids() == [] and t.position(now=1) == 3


def test_ids_in_any_order_in_one_read():
    t = tracker()
    t.saw([5, 1, 3], now=0)
    assert t.open_ids() == [2, 4] and t.head == 5


def test_a_gap_holds_the_position_only_for_the_hold_but_stays_watched():
    t = tracker()
    t.saw([1, 3], now=0)
    assert t.position(now=7.9) == 1
    assert t.position(now=9) == 3 and t.open_ids() == [2]


def test_after_the_ttl_a_gap_is_a_rollback():
    t = tracker()
    t.saw([1, 3], now=0)
    assert t.expire(now=59) == 0 and t.open_ids() == [2]
    assert t.expire(now=61) == 1
    assert t.open_ids() == []


def test_a_jump_is_no_gap():
    t = tracker()
    t.saw([1, 1_000_002], now=0)
    assert t.open_ids() == [] and t.position(now=0) == 1_000_002


def test_a_jump_of_exactly_max_jump_is_still_a_gap():
    t = tracker(max_jump=3)
    t.saw([1, 5], now=0)
    assert t.open_ids() == [2, 3, 4]
    t.saw([9], now=0)
    assert t.open_ids() == [2, 3, 4, 6, 7, 8]
    t.saw([14], now=0)                                  # 4 missing ids: a jump
    assert t.open_ids() == [2, 3, 4, 6, 7, 8]


def test_the_oldest_gaps_go_first_above_the_cap():
    t = tracker(max_gaps=2)
    t.saw([1, 3, 5, 7], now=0)
    t.expire(now=0)
    assert t.open_ids() == [4, 6]


def test_the_first_read_of_a_worker_starts_at_the_head_it_was_given():
    t = GapTracker(ttl=60, hold=8, max_jump=1000, max_gaps=5000, head=10)
    t.saw([12], now=0)
    assert t.open_ids() == [11]


def test_a_row_at_or_below_the_head_that_was_no_gap_changes_nothing():
    t = tracker(head=10)
    t.saw([4], now=0)
    assert t.head == 10 and t.open_ids() == [] and t.position(now=0) == 10
