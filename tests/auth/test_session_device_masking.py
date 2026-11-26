"""Session details of another user are masked (owner decisions 2026-10-29, audit L3): only the user themself sees the
address and the client of their sessions; anyone else sees that a session exists (id, status, times: enough to end
it) with the address cut to its network and no user agent."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1 import V1
from app.backend.api.v1.models.response.auth import masked_ip_address
from app.backend.enums.general import UserType
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.models.user import UserSession
from tests.helpers import give, make_admin, make_user

AGENT = "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0"


async def _session(db, user, ip_address="192.168.17.42") -> UserSession:
    now = datetime.now(timezone.utc)
    row = UserSession(id=uuid.uuid4().hex, user_id=user.id, last_activity=now, created_at=now,
                      expires_at=now + timedelta(hours=1), ip_address=ip_address, user_agent=AGENT)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _of(sessions, row):
    (found,) = [item for item in sessions if item["id"] == row.id]
    return found


@pytest.mark.parametrize("value, masked", [
    ("192.168.17.42", "192.168.0.0/16"),
    ("10.1.2.3", "10.1.0.0/16"),
    ("2001:db8:1:2::5", "2001:db8:1::/48"),
    ("::ffff:192.168.17.42", "192.168.0.0/16"),
    ("fe80::1%eth0", "fe80::/48"),
    ("not an address", None),
    ("192.168.17.42:5000", None),
    ("", None),
    (None, None),
])
def test_masked_ip_address(value, masked):
    assert masked_ip_address(value) == masked


async def test_another_users_sessions_are_masked(client, db, headers):
    admin, user = await make_admin(db), await make_user(db)
    row = await _session(db, user)
    listed = await client.get(V1.path(UserRoutes.USER_SESSIONS, user_id=user.id), headers=headers(admin))
    assert listed.status_code == 200, listed.text
    item = _of(listed.json(), row)
    assert item["ip_address"] == "192.168.0.0/16" and item["user_agent"] is None and item["device_masked"] is True
    assert item["status"] == "online" and item["created_at"] and item["expires_at"]


async def test_the_user_sees_their_own_sessions_in_clear(client, db, headers):
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, user, ["user.view"])
    row = await _session(db, user, ip_address="2001:db8:1:2::5")
    listed = await client.get(V1.path(UserRoutes.USER_SESSIONS, user_id=user.id), headers=headers(user))
    assert listed.status_code == 200, listed.text
    item = _of(listed.json(), row)
    assert item["ip_address"] == "2001:db8:1:2::5" and item["user_agent"] == AGENT and item["device_masked"] is False

    own = await client.get(V1.path(AuthRoutes.SESSIONS), headers=headers(user))
    assert own.status_code == 200, own.text
    item = _of(own.json()["sessions"], row)
    assert item["ip_address"] == "2001:db8:1:2::5" and item["user_agent"] == AGENT


async def test_ending_another_users_session_answers_it_masked(client, db, headers):
    admin, user = await make_admin(db), await make_user(db)
    row = await _session(db, user)
    ended = await client.post(V1.path(UserRoutes.USER_SESSION_END, user_id=user.id, session_id=row.id),
                              headers=headers(admin))
    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert body["id"] == row.id and body["status"] == "ended"
    assert body["ip_address"] == "192.168.0.0/16" and body["user_agent"] is None and body["device_masked"] is True


async def test_ending_an_own_session_answers_it_in_clear(client, db, headers):
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, user, ["user.view", "user.update"])
    row = await _session(db, user)
    ended = await client.post(V1.path(UserRoutes.USER_SESSION_END, user_id=user.id, session_id=row.id),
                              headers=headers(user))
    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert body["ip_address"] == "192.168.17.42" and body["user_agent"] == AGENT and body["device_masked"] is False
