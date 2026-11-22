"""The event catalog: every event fits its column and carries no free text"""
import json
from typing import get_args

import pytest

from app.backend.config import EventsConfig
from app.backend.enums.events import MOVE_STATUSES, PAYLOAD_LIMIT, EventType, StreamCloseReason
from app.backend.enums.general import MoveStatus


def _largest(annotation):
    if annotation is int:
        return 2 ** 63
    if annotation is str:
        pytest.fail("an event param is never a plain str: use an enum's values as a Literal")
    return max(get_args(annotation), key=len)


@pytest.mark.parametrize("event", list(EventType))
def test_the_largest_payload_fits_the_column(event):
    payload = {name: _largest(annotation) for name, annotation in event.params.items()}
    assert len(json.dumps(payload, separators=(",", ":"))) <= PAYLOAD_LIMIT


@pytest.mark.parametrize("event", list(EventType))
def test_a_code_is_resource_dot_action(event):
    resource, _, action = event.value.partition(".")
    assert resource and action and event.value == event.value.lower()
    assert len(event.value) <= 48                  # outbox_events.type


def test_close_reasons_are_stable():
    assert {r.value for r in StreamCloseReason} == {"token_expired", "session_ended", "restart"}


def test_move_statuses_are_the_domain_enum():
    assert set(get_args(MOVE_STATUSES)) == {status.value for status in MoveStatus}


def test_a_replay_fits_the_queue():
    with pytest.raises(ValueError):
        EventsConfig(replay_max=256, queue_max=256)
    assert EventsConfig().replay_max < EventsConfig().queue_max
