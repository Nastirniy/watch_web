"""Deployment files of the PKI: the signer container, the API mounts, the env examples and the systemd units"""
import os
import re
from pathlib import Path

import pytest
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
    assert signer["pull_policy"] == "never"
    assert "PKI_SIGNER_UID" in signer["user"]
    assert signer["restart"] == "unless-stopped"


def test_socket_group_is_its_own():
    """The signer runs with the socket's own group and the api joins it; never MEDIA_GID, which host accounts
    (the nginx user) may share"""
    assert PKI["pki-signer"]["user"] == \
        "${PKI_SIGNER_UID:?set PKI_SIGNER_UID in .env}:${PKI_SOCKET_GID:?set PKI_SOCKET_GID in .env}"
    assert PKI["api"]["group_add"] == ["${PKI_SOCKET_GID:?set PKI_SOCKET_GID in .env}"]
    assert "MEDIA_GID" not in PKI_TEXT.split("services:", 1)[1]


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


def test_crl_reload_survives_a_burst_of_revocations():
    """Every revocation publishes the CRL and the path unit starts the reload once per publish. Under systemd's
    default start limit (5 starts in 10 s) a burst of revocations failed the service with start-limit-hit, and the
    path unit failed with it and stopped watching for good: nginx never re-read the CRL again, so every certificate
    revoked afterwards kept passing nginx (found on the test server, 2026-10-19)"""
    service = (SYSTEMD / "watch-web-crl.service").read_text(encoding="utf-8")
    unit_section = service.split("[Service]")[0]
    assert "[Unit]" in unit_section and re.search(r"^StartLimitIntervalSec=0$", unit_section, re.MULTILINE)


def test_signer_env_example_names_every_signer_setting():
    text = (ROOT / "signer.env.example").read_text(encoding="utf-8")
    for name in SignerSettings.model_fields:
        assert f"SIGNER_{name.upper()}=" in text, name


def test_env_example_documents_pki():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("PKI_DIR=", "PKI_SIGNER_UID=", "PKI_SOCKET_GID=",
                 "COMPOSE_FILE=docker-compose.yml:docker-compose.pki.yml"):
        assert name in text, name


def test_deployment_files_are_ascii():
    for relative in ("docker-compose.pki.yml", "signer.env.example", ".env.example", "nginx.conf",
                     "deploy/pki/remove-old-root.cmd", "deploy/systemd/watch-web-crl.path"):
        (ROOT / relative).read_bytes().decode("ascii")


def test_signer_env_stays_out_of_git_and_the_image():
    for name in (".gitignore", ".dockerignore"):
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        assert ".env" in lines and "signer.env" in lines, name


THUMBPRINT = "0123456789abcdefABCDEF0123456789abcdef01"


def _remove_old_root(tmp_path, *args):
    """deploy/pki/remove-old-root.cmd as a dry run: certutil only echoed, the certificate stores never touched"""
    import subprocess
    script = tmp_path / "remove-old-root.cmd"
    script.write_bytes((ROOT / "deploy" / "pki" / "remove-old-root.cmd").read_bytes()
                       .replace(b"certutil -", b"echo certutil -"))
    return subprocess.run(["cmd", "/c", str(script), *args], capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe")
def test_remove_old_root_takes_only_a_thumbprint(tmp_path):
    """certutil's CertId also matches an index, a serial or a CN: anything but 40 hex characters is refused"""
    for args in ((), ("0",), ("My Root CA",), (THUMBPRINT[:-1],), (THUMBPRINT + "0",), (THUMBPRINT[:-1] + "g",),
                 ("01:23:45:67:89:ab:cd:ef:01:23:45:67:89:ab:cd:ef:01:23:45:67",)):
        result = _remove_old_root(tmp_path, *args)
        output = result.stdout + result.stderr
        assert result.returncode == 1, (args, output)
        assert "Usage" in output and "certutil" not in output, (args, output)
    machine = _remove_old_root(tmp_path, THUMBPRINT)
    assert machine.returncode == 0, machine.stdout + machine.stderr
    assert f'certutil -delstore Root "{THUMBPRINT}"' in machine.stdout
    user = _remove_old_root(tmp_path, THUMBPRINT, "user")
    assert user.returncode == 0, user.stdout + user.stderr
    assert f'certutil -user -delstore Root "{THUMBPRINT}"' in user.stdout
