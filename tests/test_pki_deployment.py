"""Deployment files of the PKI: the signer container, the API mounts, the env examples and the systemd units"""
from pathlib import Path

import yaml

from app.backend.signer.settings import SignerSettings

ROOT = Path(__file__).resolve().parent.parent
BASE_TEXT = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
PKI_TEXT = (ROOT / "docker-compose.pki.yml").read_text(encoding="utf-8")
BASE = yaml.safe_load(BASE_TEXT)["services"]
PKI = yaml.safe_load(PKI_TEXT)["services"]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_base_compose_works_without_pki_dir():
    assert "PKI_DIR" not in BASE_TEXT and "pki-signer" not in BASE_TEXT
    assert "docker-compose.pki.yml" in BASE_TEXT


def test_signer_has_no_network_and_its_own_env():
    signer = PKI["pki-signer"]
    assert signer["network_mode"] == "none"
    assert signer["env_file"] == "signer.env"
    assert signer["command"][-1] == "serve"
    assert signer["image"] == "watch-web"
    assert "PKI_SIGNER_UID" in signer["user"]
    assert signer["restart"] == "unless-stopped"


def test_only_the_signer_mounts_the_private_key():
    private = [name for name, service in PKI.items() if any("/private" in v for v in service.get("volumes", []))]
    assert private == ["pki-signer"]
    signer_volumes = PKI["pki-signer"]["volumes"]
    assert any(v.endswith("/ca:ro") for v in signer_volumes)
    assert any("/private" in v and v.endswith(":ro") for v in signer_volumes)
    assert any("/run" in v for v in signer_volumes)
    api_volumes = PKI["api"]["volumes"]
    for part in ("/ca", "/crl", "/issued", "/run"):
        assert any(part in v for v in api_volumes), part
    assert any("/ca" in v and v.endswith(":ro") for v in api_volumes)
    assert "depends_on" not in PKI["api"], "the API starts without the signer"
    for name in ("media-worker", "imgproxy"):
        assert name not in PKI
        assert "PKI_DIR" not in str(BASE[name])


def test_api_pki_settings_point_into_pki_dir():
    env = PKI["api"]["environment"]
    assert env["MYSETTINGS_PKI__CA_CERT_PATH"] == "${PKI_DIR:?set PKI_DIR in .env}/ca/client-ca.crt"
    assert env["MYSETTINGS_PKI__SIGNER_SOCKET"].endswith("/run/signer.sock")
    assert env["MYSETTINGS_PKI__STORE_DIR"].endswith("/issued")
    assert env["MYSETTINGS_PKI__CRL_PATH"].endswith("/crl/nginx.crl")
    assert all(value.startswith("${PKI_DIR") for value in env.values())


def test_systemd_units():
    path = (SYSTEMD / "watch-web-crl.path").read_text(encoding="utf-8")
    assert "PathChanged=" in path and "nginx.crl" in path and "Unit=watch-web-crl.service" in path
    service = (SYSTEMD / "watch-web-crl.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "nginx -t && nginx -s reload" in service
    timer = (SYSTEMD / "watch-web-crl-refresh.timer").read_text(encoding="utf-8")
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
    refresh = (SYSTEMD / "watch-web-crl-refresh.service").read_text(encoding="utf-8")
    assert "app.backend.cli.pki crl" in refresh and "exec -T api" in refresh


def test_signer_env_example_names_every_signer_setting():
    text = (ROOT / "signer.env.example").read_text(encoding="utf-8")
    for name in SignerSettings.model_fields:
        assert f"SIGNER_{name.upper()}=" in text, name


def test_env_example_documents_pki():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("PKI_DIR=", "PKI_SIGNER_UID=", "COMPOSE_FILE=docker-compose.yml:docker-compose.pki.yml"):
        assert name in text, name


def test_deployment_files_are_ascii():
    for relative in ("docker-compose.pki.yml", "signer.env.example", ".env.example", "nginx.conf",
                     "deploy/pki/remove-old-root.cmd", "deploy/systemd/watch-web-crl.path"):
        (ROOT / relative).read_bytes().decode("ascii")
