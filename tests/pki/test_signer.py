"""The signing service: fixed profile, refusals, CRLs, health, start-up checks, import boundary and commands"""
import os
import re
import socket
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from fastapi.routing import APIRoute
from loguru import logger

from app.backend.enums.general import RevocationReason
from app.backend.enums.routes import SignerRoutes
from app.backend.pki.authority import CaCheckError, sha256_fingerprint
from app.backend.signer.__main__ import bind_unix_socket, main
from app.backend.signer.app import create_app
from app.backend.signer.settings import SignerSettings
from tests.paths import PROJECT_ROOT
from tests.pki_helpers import client_ca, legacy_root, pem, write_ca_files
from tests.api.test_route_registry import _imported_modules

APP_ROOT = PROJECT_ROOT


def public_pem(bits: int) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def ec_public_pem() -> str:
    return ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


_RSA_2048 = public_pem(2048)


def valid_body() -> dict:
    return {"public_key_pem": _RSA_2048, "label": "Laptop", "validity_days": 30}


def settings_for(tmp_path, **extra) -> SignerSettings:
    cert_path, key_path = write_ca_files(tmp_path, client_ca())
    return SignerSettings(ca_cert_path=str(cert_path), ca_key_path=str(key_path), **extra)


@pytest.fixture
async def signer(tmp_path):
    app = create_app(settings_for(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signer") as client:
        yield client


async def test_sign_client_uses_the_fixed_profile(signer):
    body = {**valid_body(), "extensions": [{"oid": "2.5.29.19", "ca": True}],
            "issuer": "CN=Evil", "subject": "CN=bank.example"}
    r = await signer.post(SignerRoutes.SIGN_CLIENT.value, json=body)
    assert r.status_code == 200
    cert = x509.load_pem_x509_certificate(r.json()["certificate_pem"].encode())
    assert cert.issuer == client_ca()[0].subject and r.json()["serial"] == format(cert.serial_number, "x")
    assert cert.subject.rfc4514_string() == "CN=Laptop"
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.CLIENT_AUTH]


@pytest.mark.parametrize("change, reason", [
    ({"public_key_pem": ec_public_pem()}, "public_key"), ({"public_key_pem": public_pem(1024)}, "public_key"),
    ({"public_key_pem": "not a key"}, "public_key"),
    ({"label": ""}, "label"), ({"label": "a" * 65}, "label"), ({"label": " padded"}, "label"),
    ({"validity_days": 0}, "validity_days"), ({"validity_days": 731}, "validity_days")])
async def test_client_refusals(signer, change, reason):
    r = await signer.post(SignerRoutes.SIGN_CLIENT.value, json={**valid_body(), **change})
    assert r.status_code == 422 and r.json()["reason"] == reason
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["title"] == "Signing refused" and r.json()["status"] == 422


def crl_body(**change) -> dict:
    now = datetime.now(timezone.utc)
    return {"crl_number": 1, "this_update": now.isoformat(), "next_update": (now + timedelta(days=7)).isoformat(),
            "entries": [], **change}


async def test_crl_refusals(signer):
    now = datetime.now(timezone.utc)
    r = await signer.post(SignerRoutes.SIGN_CRL.value, json=crl_body(crl_number=0))
    assert r.status_code == 422 and r.json()["reason"] == "crl_number"
    for next_update in (now - timedelta(days=1), now + timedelta(days=32)):
        r = await signer.post(SignerRoutes.SIGN_CRL.value, json=crl_body(next_update=next_update.isoformat()))
        assert r.status_code == 422 and r.json()["reason"] == "crl_dates"
    same = now.isoformat()
    r = await signer.post(SignerRoutes.SIGN_CRL.value, json=crl_body(this_update=same, next_update=same))
    assert r.status_code == 422 and r.json()["reason"] == "crl_dates"


async def test_sign_crl(signer):
    revoked_at = datetime.now(timezone.utc).replace(microsecond=0)
    entry = {"serial": "0a1b", "revoked_at": revoked_at.isoformat(), "reason": RevocationReason.KEY_COMPROMISE.value}
    r = await signer.post(SignerRoutes.SIGN_CRL.value, json=crl_body(crl_number=5, entries=[entry]))
    assert r.status_code == 200
    crl = x509.load_pem_x509_crl(r.json()["crl_pem"].encode())
    assert crl.is_signature_valid(client_ca()[0].public_key())
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 5
    assert len(crl) == 1
    assert crl[0].serial_number == 0x0A1B
    assert crl[0].extensions.get_extension_for_class(x509.CRLReason).value.reason == x509.ReasonFlags.key_compromise


async def test_health(signer):
    r = await signer.get(SignerRoutes.HEALTH.value)
    assert r.status_code == 200
    cert = client_ca()[0]
    body = r.json()
    assert body["subject"] == cert.subject.rfc4514_string()
    assert datetime.fromisoformat(body["not_after"]) == cert.not_valid_after_utc
    assert body["fingerprint_sha256"] == sha256_fingerprint(cert)


def test_refuses_to_start_on_a_bad_ca(tmp_path):
    ca = client_ca()
    leaf_dir = tmp_path / "leaf"
    leaf_dir.mkdir()
    from tests.pki_helpers import make_leaf
    leaf = make_leaf(ca)
    cert_path, key_path = write_ca_files(leaf_dir, leaf)
    with pytest.raises(CaCheckError) as raised:
        create_app(SignerSettings(ca_cert_path=str(cert_path), ca_key_path=str(key_path)))
    assert raised.value.reason == "not_a_ca"

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    cert_path, _ = write_ca_files(other_dir, ca)
    _, other_key = write_ca_files(tmp_path, legacy_root())
    with pytest.raises(CaCheckError) as raised:
        create_app(SignerSettings(ca_cert_path=str(cert_path), ca_key_path=str(other_key)))
    assert raised.value.reason == "key_mismatch"

    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    cert_path, key_path = write_ca_files(locked_dir, ca, password="secret")
    for password in ("", "wrong"):
        with pytest.raises(CaCheckError) as raised:
            create_app(SignerSettings(ca_cert_path=str(cert_path), ca_key_path=str(key_path), ca_key_password=password))
        assert raised.value.reason == "key_password"
    create_app(SignerSettings(ca_cert_path=str(cert_path), ca_key_path=str(key_path), ca_key_password="secret"))


def test_every_route_is_an_enum_member(tmp_path):
    app = create_app(settings_for(tmp_path))
    assert {r.path for r in app.routes if isinstance(r, APIRoute)} == {m.value for m in SignerRoutes}
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None


def test_signer_never_imports_app_internals():
    """The signer runs without the app's settings: it reaches only signer, pki and enums"""
    allowed = re.compile(r"app\.backend\.(signer|pki|enums)(\.|$)")
    seen, pending, offenders = set(), [APP_ROOT / "app" / "backend" / "signer"], []
    files = []
    while pending:
        item = pending.pop()
        files += sorted(item.rglob("*.py")) if item.is_dir() else [item]
        while files:
            path = files.pop()
            if path in seen:
                continue
            seen.add(path)
            for line, name in _imported_modules(path, root=APP_ROOT):
                if not name.startswith("app."):
                    continue
                if not allowed.match(name):
                    offenders.append(f"{path.relative_to(APP_ROOT)}:{line} imports {name}")
                    continue
                target = APP_ROOT / Path(*name.split("."))
                for candidate in (target.with_suffix(".py"), target / "__init__.py"):
                    if candidate.exists():
                        pending.append(candidate)
    assert not offenders, offenders
    assert any(p.name == "app.py" for p in seen)


async def test_every_signature_is_logged(signer):
    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        await signer.post(SignerRoutes.SIGN_CLIENT.value, json=valid_body())
        await signer.post(SignerRoutes.SIGN_CRL.value, json=crl_body())
    finally:
        logger.remove(sink)
    text = [str(line).strip() for line in lines]
    assert len([t for t in text if t.startswith("signed certificate serial=")]) == 1
    assert len([t for t in text if t.startswith("signed crl number=1 entries=0")]) == 1
    assert "cn=Laptop" in text[0] and "sha256=" in text[0]


def test_cli_init_writes_once(tmp_path, monkeypatch):
    cert_path, key_path = tmp_path / "ca.crt", tmp_path / "ca.key"
    monkeypatch.setenv("SIGNER_CA_CERT_PATH", str(cert_path))
    monkeypatch.setenv("SIGNER_CA_KEY_PATH", str(key_path))
    monkeypatch.setenv("SIGNER_CA_KEY_PASSWORD", "pw")
    assert main(["init", "--cn", "Test client CA", "--days", "30"]) == 0
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert cert.subject.rfc4514_string() == "CN=Test client CA"
    assert b"ENCRYPTED" in key_path.read_bytes()
    before = (cert_path.read_bytes(), key_path.read_bytes())
    assert main(["init"]) == 1
    assert (cert_path.read_bytes(), key_path.read_bytes()) == before
    from app.backend.pki.authority import load_ca
    load_ca(str(cert_path), str(key_path), "pw")


def test_cli_sign_crl_for_another_ca(tmp_path):
    root = legacy_root()
    cert_path, key_path = write_ca_files(tmp_path, root, password="pw")
    password_file = tmp_path / "pw.txt"
    password_file.write_text("pw\n", encoding="utf-8")
    out = tmp_path / "root.crl"
    next_update = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    code = main(["sign-crl", "--ca-cert", str(cert_path), "--ca-key", str(key_path),
                 "--key-password-file", str(password_file), "--next-update", next_update,
                 "--out", str(out), "--serial", "0a1b"])
    assert code == 0
    crl = x509.load_pem_x509_crl(out.read_bytes())
    assert crl.is_signature_valid(root[0].public_key())
    assert [entry.serial_number for entry in crl] == [0x0A1B]
    assert crl.next_update_utc.strftime("%Y-%m-%d") == next_update
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 1


def test_cli_sign_crl_never_leaves_a_partial_file(tmp_path, monkeypatch):
    """sign-crl writes a temporary file next to --out and renames it: a failed write leaves the previous CRL whole
    and no temporary file"""
    cert_path, key_path = write_ca_files(tmp_path, legacy_root())
    out_dir = tmp_path / "crl"
    out_dir.mkdir()
    out = out_dir / "legacy-root.crl"
    out.write_bytes(b"the previous CRL")
    next_update = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    args = ["sign-crl", "--ca-cert", str(cert_path), "--ca-key", str(key_path), "--next-update", next_update,
            "--out", str(out)]

    def disk_full(*_):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", disk_full)
        assert main(args) == 1
    assert out.read_bytes() == b"the previous CRL"
    assert [p.name for p in out_dir.iterdir()] == [out.name]

    assert main(args) == 0
    assert [p.name for p in out_dir.iterdir()] == [out.name]
    assert x509.load_pem_x509_crl(out.read_bytes()).is_signature_valid(legacy_root()[0].public_key())


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets")
def test_serve_socket_is_owner_and_group_only(tmp_path):
    """serve binds the socket itself (uvicorn chmods a path it binds to 0666): 0660 whatever the umask, and the
    stale socket a killed signer leaves behind is replaced"""
    path = str(tmp_path / "signer.sock")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)
    stale.close()
    assert stat.S_ISSOCK(os.stat(path).st_mode)
    old_umask = os.umask(0)
    try:
        sock = bind_unix_socket(path)
    finally:
        os.umask(old_umask)
    try:
        assert stat.S_ISSOCK(os.stat(path).st_mode)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o660
        sock.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        client.close()
    finally:
        sock.close()
