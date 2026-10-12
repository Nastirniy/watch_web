"""Commands of the signing service: serve, init, sign-crl"""
import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from loguru import logger

from app.backend.enums.general import RevocationReason
from app.backend.pki.authority import CaCheckError
from app.backend.pki.crl import CrlEntry, build_crl
from app.backend.pki.profile import build_client_ca
from app.backend.signer.settings import SignerSettings


def _serve() -> int:
    import uvicorn

    from app.backend.signer.app import create_app

    logger.remove()
    logger.add(sys.stderr, diagnose=False)
    settings = SignerSettings()
    try:
        app = create_app(settings)
    except CaCheckError as exc:
        print(f"signer: the CA is not usable: {exc.reason}", file=sys.stderr)
        return 1
    if not settings.socket_path:
        print("signer: SIGNER_SOCKET_PATH is not set", file=sys.stderr)
        return 1
    try:
        os.unlink(settings.socket_path)  # a stale socket from a previous run
    except FileNotFoundError:
        pass
    os.umask(0o117)  # the socket is created 0660: owner and group only
    uvicorn.run(app, uds=settings.socket_path)
    return 0


def _init(common_name: str, days: int) -> int:
    settings = SignerSettings()
    if os.path.exists(settings.ca_cert_path) or os.path.exists(settings.ca_key_path):
        print("signer: a CA file already exists, nothing written", file=sys.stderr)
        return 1
    cert, key = build_client_ca(common_name, days)
    encryption = (serialization.BestAvailableEncryption(settings.ca_key_password.encode("utf-8"))
                  if settings.ca_key_password else serialization.NoEncryption())
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    # The key file is created 0400 from the start, never readable wider than that
    descriptor = os.open(settings.ca_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key_pem)
    descriptor = os.open(settings.ca_cert_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(cert_pem)
    print(f"signer: client CA written to {settings.ca_cert_path} and {settings.ca_key_path}")
    return 0


def _load_any_authority(cert_path: str, key_path: str, password: str):
    """Certificate and key of the existing root, which may have no extensions (so not load_ca); they must match"""
    try:
        with open(cert_path, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
        with open(key_path, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        raise CaCheckError("unreadable") from None
    try:
        key = serialization.load_pem_private_key(data, password.encode("utf-8") if password else None)
    except (TypeError, ValueError):
        raise CaCheckError("key_password") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CaCheckError("unreadable")
    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise CaCheckError("key_mismatch")
    return cert, key


def _sign_crl(args: argparse.Namespace) -> int:
    password = ""
    if args.key_password_file:
        with open(args.key_password_file, encoding="utf-8") as handle:
            password = handle.read().rstrip("\r\n")
    try:
        cert, key = _load_any_authority(args.ca_cert, args.ca_key, password)
    except CaCheckError as exc:
        print(f"signer: the CA is not usable: {exc.reason}", file=sys.stderr)
        return 1
    try:
        next_update = datetime.strptime(args.next_update, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print("signer: --next-update must be YYYY-MM-DD", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    if next_update <= now:
        print("signer: --next-update must be in the future", file=sys.stderr)
        return 1
    try:
        entries = [CrlEntry(int(serial, 16), now, RevocationReason.UNSPECIFIED) for serial in args.serial]
    except ValueError:
        print("signer: --serial must be hex", file=sys.stderr)
        return 1
    crl = build_crl(cert, key, args.number, now, next_update, entries)
    with open(args.out, "wb") as handle:
        handle.write(crl.public_bytes(serialization.Encoding.PEM))
    print(f"signer: CRL number {args.number} with {len(entries)} entries written to {args.out}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backend.signer")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the signing API on SIGNER_SOCKET_PATH")
    init = commands.add_parser("init", help="make the client CA at SIGNER_CA_CERT_PATH / SIGNER_CA_KEY_PATH")
    init.add_argument("--cn", default="watch_web client CA")
    init.add_argument("--days", type=int, default=3650)
    sign = commands.add_parser("sign-crl", help="sign a CRL with any CA (the existing root)")
    sign.add_argument("--ca-cert", required=True)
    sign.add_argument("--ca-key", required=True)
    sign.add_argument("--key-password-file")
    sign.add_argument("--next-update", required=True, help="YYYY-MM-DD")
    sign.add_argument("--out", required=True)
    sign.add_argument("--number", type=int, default=1)
    sign.add_argument("--serial", nargs="*", default=[], help="hex serials to list as revoked")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve()
    if args.command == "init":
        return _init(args.cn, args.days)
    return _sign_crl(args)


if __name__ == "__main__":
    sys.exit(main())
