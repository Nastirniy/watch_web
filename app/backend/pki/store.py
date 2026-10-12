"""The certificate folder: <store>/<user_id>/<certificate_id>/ with the files of one package"""
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CertificateFiles:
    certificate_pem: bytes
    p12: bytes
    password: str
    info: str


def _write(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)  # the umask may have narrowed the mode given to open()


def write_certificate_dir(store_dir: Path, user_id: int, certificate_id: int, files: CertificateFiles) -> str:
    """Write the folder through a .tmp- directory renamed into place; returns "<user_id>/<certificate_id>".

    Raises FileExistsError when the folder exists; the temporary directory is removed on any failure."""
    user_dir = store_dir / str(user_id)
    target = user_dir / str(certificate_id)
    if target.exists():
        raise FileExistsError(str(target))
    user_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = user_dir / f".tmp-{certificate_id}-{secrets.token_hex(4)}"
    temp.mkdir(mode=0o700)
    try:
        os.chmod(temp, 0o700)
        _write(temp / "certificate.p12", files.p12, 0o600)
        _write(temp / "password.txt", files.password.encode("utf-8"), 0o600)
        _write(temp / "certificate.crt", files.certificate_pem, 0o644)
        _write(temp / "info.txt", files.info.encode("utf-8"), 0o644)
        os.rename(temp, target)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return f"{user_id}/{certificate_id}"


def remove_certificate_dir(store_dir: Path, files_dir: str) -> None:
    """Remove a certificate folder; a missing one is fine"""
    shutil.rmtree(store_dir / files_dir, ignore_errors=True)


def read_package(store_dir: Path, files_dir: str) -> tuple[bytes, str]:
    """The .p12 bytes and the password; FileNotFoundError when the folder is gone"""
    folder = store_dir / files_dir
    return (folder / "certificate.p12").read_bytes(), (folder / "password.txt").read_text(encoding="utf-8")


def write_atomically(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write `<path>.tmp` in the same directory, then replace `path` with it"""
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.unlink(missing_ok=True)
        _write(temp, data, mode)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
