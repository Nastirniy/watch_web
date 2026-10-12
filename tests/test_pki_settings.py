"""settings.pki: the start-up checks and GET /certificates/authority"""
import pytest

from app.backend.api.v1 import V1
from app.backend.bootstrap.pki import PkiSettingsError, check_pki_settings
from app.backend.config import PkiConfig
from app.backend.enums.routes import CertificateRoutes
from app.backend.services.crl_publisher import publish_crl
from tests.helpers import make_user
from tests.pki_helpers import client_ca, legacy_root, make_leaf, pem, write_ca_files

AUTHORITY = V1.path(CertificateRoutes.AUTHORITY)


def configured(tmp_path, **values) -> PkiConfig:
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir(exist_ok=True)
    cert_path, _ = write_ca_files(ca_dir, client_ca())
    (tmp_path / "issued").mkdir(exist_ok=True)
    (tmp_path / "crl").mkdir(exist_ok=True)
    base = dict(ca_cert_path=str(cert_path), signer_socket=str(tmp_path / "s.sock"), store_dir=str(tmp_path / "issued"),
                crl_path=str(tmp_path / "crl" / "nginx.crl"))
    return PkiConfig(**{**base, **values})


def test_partial_configuration_refuses_to_start(tmp_path):
    with pytest.raises(PkiSettingsError) as error:
        check_pki_settings(PkiConfig(store_dir=str(tmp_path)))
    for name in ("ca_cert_path", "signer_socket", "crl_path"):
        assert name in str(error.value)
    assert "store_dir" not in str(error.value)


def test_bad_ca_certificate_refuses_to_start(tmp_path):
    leaf = tmp_path / "leaf.crt"
    leaf.write_bytes(pem(make_leaf(client_ca())[0]))
    with pytest.raises(PkiSettingsError, match="ca_cert_path"):
        check_pki_settings(configured(tmp_path, ca_cert_path=str(leaf)))


def test_unwritable_folders_refuse_to_start(tmp_path):
    with pytest.raises(PkiSettingsError, match="store_dir"):
        check_pki_settings(configured(tmp_path, store_dir=str(tmp_path / "missing")))
    with pytest.raises(PkiSettingsError, match="crl_path"):
        check_pki_settings(configured(tmp_path, crl_path=str(tmp_path / "missing" / "nginx.crl")))


def test_unparsable_extra_crl_refuses_to_start(tmp_path):
    bad = tmp_path / "bad.crl"
    bad.write_bytes(b"not a crl")
    with pytest.raises(PkiSettingsError, match="extra_crl_paths"):
        check_pki_settings(configured(tmp_path, extra_crl_paths=[str(bad)]))
    with pytest.raises(PkiSettingsError, match="extra_crl_paths"):
        check_pki_settings(configured(tmp_path, extra_crl_paths=[str(tmp_path / "missing.crl")]))


def test_a_configured_pki_passes(tmp_path):
    check_pki_settings(configured(tmp_path))


def test_not_configured_is_fine():
    check_pki_settings(PkiConfig())


async def test_authority_route(client, db, headers, pki):
    user = await make_user(db)
    await publish_crl(db)
    response = await client.get(AUTHORITY, headers=headers(user))
    assert response.status_code == 200
    body = response.json()
    ca = client_ca()[0]
    assert body["subject"] == ca.subject.rfc4514_string()
    assert body["fingerprint_sha256"].count(":") == 31
    assert body["certificate_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert body["crl"]["revoked_count"] == 0
    assert body["crl"]["this_update"] and body["crl"]["next_update"]


async def test_authority_route_without_pki(client, db, headers):
    user = await make_user(db)
    response = await client.get(AUTHORITY, headers=headers(user))
    assert response.status_code == 503
    assert response.json()["code"] == "certificate_authority_not_configured"


async def test_pki_cli_crl_and_status(db, pki, session_maker, monkeypatch, capsys):
    from app.backend.cli import pki as cli

    async def maker():
        return session_maker
    monkeypatch.setattr(cli, "get_session_maker", maker)
    assert await cli.run(["crl"]) == 0
    assert pki.crl_path.exists()
    assert await cli.run(["status"]) == 0
    output = capsys.readouterr().out
    assert "signer: reachable" in output and "CRL stale: no" in output
    pki.signer_down()
    assert await cli.run(["crl"]) == 1


async def test_pki_cli_without_configuration(db, capsys):
    from app.backend.cli import pki as cli
    assert await cli.run(["crl"]) == 1
