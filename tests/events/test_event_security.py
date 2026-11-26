"""security.certificate_mismatch: a valid token of a session presented with another client certificate tells the
holders of audit.view (a system permission, in effect at the root), in the transaction of the audit row and exactly
as often as that row is written. Ids only: never a token, a fingerprint or a certificate value."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select, update

from app.backend.api.v1 import V1
from app.backend.auth.context import load_auth_context
from app.backend.config import EventsConfig, settings
from app.backend.enums.audit import AuditEvent
from app.backend.enums.events import EventType
from app.backend.enums.general import Permissions as P, UserType
from app.backend.enums.routes import AuthRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.outbox_event import OutboxEvent
from app.backend.services.events.hub import EventHub
from tests.helpers import give, make_admin, make_certificate, make_certificate_row, make_post, make_user, nginx_headers
from tests.events.test_event_hub import TextRenderer, later, queued

LOGIN = V1.path(AuthRoutes.LOGIN)
REFRESH = V1.path(AuthRoutes.REFRESH)
ME = V1.path(AuthRoutes.ME)
MISMATCH = EventType.SECURITY_CERTIFICATE_MISMATCH.value


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)


async def _owner_with_certificate(db, cert):
    """An ordinary user (no audit.view) who logs in with cert"""
    post = await make_post(db)
    owner = await make_user(db, [P.POST_VIEW], post_id=post.id)
    await make_certificate_row(db, owner, cert=cert)
    return owner


async def _tokens(client, cert) -> dict:
    response = await client.post(LOGIN, headers=nginx_headers(cert))
    assert response.status_code == 200, response.text
    return response.json()


async def _events(session_maker) -> list:
    async with session_maker() as fresh:
        return list((await fresh.exec(select(OutboxEvent).where(OutboxEvent.type == MISMATCH)
                                      .order_by(OutboxEvent.id))).all())


async def _audit_rows(session_maker) -> list:
    async with session_maker() as fresh:
        return list((await fresh.exec(select(AuditLog).where(
            AuditLog.event == AuditEvent.SESSION_CERTIFICATE_MISMATCH.code).order_by(AuditLog.id))).all())


async def test_one_event_with_each_audit_row_never_more(client, db, production, session_maker, events_on):
    own, other = make_certificate("cn-ev-own", 0x1A1A), make_certificate("cn-ev-other", 0x2B2B)
    owner = await _owner_with_certificate(db, own)
    tokens = await _tokens(client, own)
    bearer = {"Authorization": f"Bearer {tokens['access_token']}"}

    for _ in range(3):
        stolen = await client.get(ME, headers={**nginx_headers(other), **bearer})
        assert stolen.status_code == 401 and stolen.json()["code"] == "token_certificate_mismatch"
    refused = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=nginx_headers(other))
    assert refused.status_code == 401 and refused.json()["code"] == "token_certificate_mismatch"

    (audit,) = await _audit_rows(session_maker)
    (event,) = await _events(session_maker)
    assert (event.scope_key, event.permission, event.user_id, event.v) == ("root", P.AUDIT_VIEW.value, None, 1)
    assert json.loads(event.payload) == {"audit_log_id": audit.id, "session_user_id": owner.id}
    for secret in (tokens["access_token"], tokens["refresh_token"], nginx_headers(other)["X-SSL-Client-Fingerprint"],
                   nginx_headers(own)["X-SSL-Client-Fingerprint"]):
        assert secret not in event.payload

    # The interval passes: the next attempt writes a second audit row, and a second event with it
    async with session_maker() as other_session:
        await other_session.exec(update(AuditLog).where(AuditLog.id == audit.id).values(
            created_at=datetime.now(timezone.utc) - timedelta(minutes=6)))
        await other_session.commit()
    refused = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}, headers=nginx_headers(other))
    assert refused.status_code == 401
    audits, events = await _audit_rows(session_maker), await _events(session_maker)
    assert len(audits) == len(events) == 2
    assert json.loads(events[1].payload) == {"audit_log_id": audits[1].id, "session_user_id": owner.id}


async def test_events_off_write_the_audit_row_only(client, db, production, session_maker):
    assert settings.events.enabled is False
    own, other = make_certificate("cn-off-own", 0x3C3C), make_certificate("cn-off-other", 0x4D4D)
    await _owner_with_certificate(db, own)
    tokens = await _tokens(client, own)
    stolen = await client.get(ME, headers={**nginx_headers(other), "Authorization": f"Bearer {tokens['access_token']}"})
    assert stolen.status_code == 401
    assert len(await _audit_rows(session_maker)) == 1
    assert await _events(session_maker) == []


async def test_only_holders_of_audit_view_receive_it(client, db, engine, production, session_maker, events_on):
    """The rights index of a stream: a system grant at the root carries audit.view; audit.view_scope at the root,
    tree permissions at the root and the owner of the token receive nothing"""
    own, other = make_certificate("cn-hub-own", 0x5E5E), make_certificate("cn-hub-other", 0x6F6F)
    owner = await _owner_with_certificate(db, own)
    system_admin = await make_admin(db)
    auditor = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await give(db, auditor, [P.AUDIT_VIEW])
    scoped_auditor = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, scoped_auditor, [P.AUDIT_VIEW_SCOPE, P.POST_VIEW])
    tree_admin = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, tree_admin, [P.POST_VIEW, P.POST_UPDATE, P.USER_VIEW, P.AUDIT_VIEW_SCOPE])

    hub = EventHub(engine, TextRenderer(), EventsConfig(enabled=True), workers=1)
    await hub.start(run_tasks=False)
    connections = {}
    for name, user in (("system_admin", system_admin), ("auditor", auditor), ("scoped_auditor", scoped_auditor),
                       ("tree_admin", tree_admin), ("owner", owner)):
        connections[name] = hub.attach(await load_auth_context(db, user, session_id=f"s{user.id}"), later(), None)

    tokens = await _tokens(client, own)
    stolen = await client.get(ME, headers={**nginx_headers(other), "Authorization": f"Bearer {tokens['access_token']}"})
    assert stolen.status_code == 401
    assert await hub.tick() == 2            # the login's session.changed (tests/events/test_event_admin.py) and the mismatch

    received = {name: [event for _, _, event, _ in queued(connection) if event != "session.changed"]
                for name, connection in connections.items()}
    assert received == {"system_admin": ["ready", MISMATCH], "auditor": ["ready", MISMATCH],
                        "scoped_auditor": ["ready"], "tree_admin": ["ready"], "owner": ["ready"]}
    (audit,) = await _audit_rows(session_maker)
    data = queued(connections["auditor"])[-1][3]
    assert (data["audit_log_id"], data["session_user_id"]) == (audit.id, owner.id)
