"""The last-administrator guard counts effective administrators (finding L4, decision 2026-10-30): active, not
archived users holding the built-in system_administrator system role and administrator at the root, whose user type
puts every permission of both in effect, and, where login requires a client certificate (every environment but
development), holding an active certificate. The guard answers 409 last_admin_required for a change that takes the
last of them away (a deactivation, an archive, a type change, an assignment removed, the last active certificate
revoked) and lets every other change through."""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1 import V1
from app.backend.auth.integrity import administrator_types, authorization_report
from app.backend.auth.privileges import holds_admin_at_root
from app.backend.config import settings
from app.backend.enums.general import UserType
from app.backend.enums.routes import SystemRoutes, UserRoutes
from app.backend.models.user_certificate import UserCertificate
from tests.helpers import make_admin, make_certificate_row, make_user

USER = V1.path(UserRoutes.USER_BY_ID)
USER_ARCHIVE = V1.path(UserRoutes.USER_ARCHIVE)
REVOKE = V1.path(UserRoutes.USER_CERTIFICATE_REVOKE)


def _code(response) -> str:
    return response.json().get("code")


async def _count(db) -> int:
    return (await authorization_report(db)).system_administrators


async def _with_certificate(db, user, **values) -> UserCertificate:
    return await make_certificate_row(db, user, **values)


def test_only_the_system_administrator_type_puts_both_roles_in_effect():
    assert administrator_types() == (UserType.SYSTEM_ADMINISTRATOR.value,)


async def test_a_type_whose_boundary_leaves_the_roles_out_does_not_count(db):
    admin = await make_admin(db)
    assert await _count(db) == 1
    admin.user_type = UserType.ADMINISTRATOR.value     # the rows stay, the boundary leaves the system role out
    db.add(admin)
    await db.commit()
    assert await _count(db) == 0
    assert not await holds_admin_at_root(db, admin.id)


async def test_in_production_an_administrator_needs_an_active_certificate(db, production):
    admin = await make_admin(db)
    assert await _count(db) == 0
    revoked = await _with_certificate(db, admin, revoked_at=datetime.now(timezone.utc))
    expired = await _with_certificate(db, admin, not_after=datetime.now(timezone.utc) - timedelta(days=1))
    assert (revoked.id, expired.id) and await _count(db) == 0
    await _with_certificate(db, admin, not_after=datetime.now(timezone.utc) + timedelta(days=30))
    assert await _count(db) == 1 and await holds_admin_at_root(db, admin.id)


async def test_in_development_the_certificate_does_not_count(db):
    await make_admin(db)
    assert await _count(db) == 1


async def test_health_counts_effective_administrators(client, db, production, monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)
    admin = await make_admin(db)
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["authorization"]["system_administrators"] == 0
    assert body["authorization"]["status"] == "degraded"
    await _with_certificate(db, admin)
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["authorization"] == {"status": "healthy", "violations": 0, "system_administrators": 1}


async def _effective_and_not(db):
    """In production: the effective administrator (with a certificate) and an administrator without one"""
    effective = await make_admin(db)
    certificate = await _with_certificate(db, effective)
    without = await make_admin(db)
    return effective, certificate, without


async def test_deactivating_the_last_effective_administrator_is_409(client, db, headers, production):
    effective, _, without = await _effective_and_not(db)
    response = await client.patch(USER.format(user_id=effective.id), headers=headers(without),
                                  data={"is_active": "false"})
    assert response.status_code == 409 and _code(response) == "last_admin_required", response.text
    await db.refresh(effective)
    assert effective.is_active


async def test_archiving_the_last_effective_administrator_is_409(client, db, headers, production):
    effective, _, without = await _effective_and_not(db)
    response = await client.post(USER_ARCHIVE.format(user_id=effective.id), headers=headers(without))
    assert response.status_code == 409 and _code(response) == "last_admin_required", response.text


async def test_a_type_change_of_the_last_effective_administrator_is_409(client, db, headers, production):
    effective, _, without = await _effective_and_not(db)
    response = await client.patch(USER.format(user_id=effective.id), headers=headers(without),
                                  data={"user_type": UserType.ADMINISTRATOR.value})
    assert response.status_code == 409 and _code(response) == "last_admin_required", response.text


async def test_revoking_the_last_active_certificate_of_the_last_one_is_409(client, db, headers, production):
    effective, certificate, without = await _effective_and_not(db)
    for caller in (without, effective):     # someone else, and the administrator themselves
        response = await client.post(REVOKE.format(user_id=effective.id, certificate_id=certificate.id),
                                     headers=headers(caller), json={})
        assert response.status_code == 409 and _code(response) == "last_admin_required", response.text
    await db.refresh(certificate)
    assert certificate.revoked_at is None


async def test_revoking_one_of_two_active_certificates_is_allowed(client, db, headers, production):
    effective, certificate, without = await _effective_and_not(db)
    await _with_certificate(db, effective)
    response = await client.post(REVOKE.format(user_id=effective.id, certificate_id=certificate.id),
                                 headers=headers(without), json={})
    assert response.status_code == 200, response.text


async def test_revoking_while_another_effective_administrator_remains_is_allowed(client, db, headers, production):
    effective, certificate, without = await _effective_and_not(db)
    await _with_certificate(db, without)
    response = await client.post(REVOKE.format(user_id=effective.id, certificate_id=certificate.id),
                                 headers=headers(without), json={})
    assert response.status_code == 200, response.text


async def test_a_change_that_takes_no_effective_administrator_away_passes(client, db, headers, production):
    """No effective administrator at all (the only one has no certificate): the guard does not block changes to other
    users, it only refuses a change that takes the last effective administrator away"""
    admin = await make_admin(db)
    user = await make_user(db)
    response = await client.patch(USER.format(user_id=user.id), headers=headers(admin), data={"is_active": "false"})
    assert response.status_code == 200, response.text
    response = await client.post(USER_ARCHIVE.format(user_id=user.id), headers=headers(admin))
    assert response.status_code == 200, response.text


async def test_in_development_an_administrator_without_a_certificate_counts(client, db, headers):
    first = await make_admin(db)
    second = await make_admin(db)
    response = await client.patch(USER.format(user_id=first.id), headers=headers(second), data={"is_active": "false"})
    assert response.status_code == 200, response.text
