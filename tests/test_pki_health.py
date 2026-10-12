"""/health: the pki part"""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.config import settings
from app.backend.enums.routes import SystemRoutes
from app.backend.pki.crl import build_crl
from app.backend.services.crl_publisher import publish_crl
from tests.pki_helpers import legacy_root, pem


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)


async def test_health_has_no_pki_part_without_configuration(client, db):
    response = await client.get(SystemRoutes.HEALTH.value)
    assert response.status_code == 200
    assert response.json()["pki"] is None


async def test_health_reports_pki(client, db, pki):
    await publish_crl(db)
    response = await client.get(SystemRoutes.HEALTH.value)
    body = response.json()
    assert body["status"] == "healthy"
    part = body["pki"]
    assert part["status"] == "healthy"
    assert part["crl_next_update_in_days"] in (6, 7)
    assert part["ca_expires_in_days"] > 3000
    assert part["crl_stale"] is False
    assert part["signer_available"] is True


async def test_health_degraded_when_the_crl_was_never_published(client, db, pki):
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["pki"]["crl_next_update_in_days"] is None
    assert body["pki"]["status"] == "degraded"
    assert body["status"] == "degraded"


async def test_health_degraded_when_the_signer_is_down(client, db, pki):
    await publish_crl(db)
    pki.signer_down()
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["pki"]["signer_available"] is False
    assert body["pki"]["status"] == "degraded"
    assert body["status"] == "degraded"


async def test_health_reports_the_earliest_next_update_of_an_extra_crl(client, db, pki, tmp_path, monkeypatch):
    root = legacy_root()
    now = datetime.now(timezone.utc)
    extra = tmp_path / "legacy-root.crl"
    extra.write_bytes(pem(build_crl(root[0], root[1], 1, now, now + timedelta(days=1, hours=1), [])))
    monkeypatch.setattr(settings.pki, "extra_crl_paths", [str(extra)])
    await publish_crl(db)
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["pki"]["crl_next_update_in_days"] == 1
    assert body["pki"]["status"] == "degraded"
    assert body["status"] == "degraded"
