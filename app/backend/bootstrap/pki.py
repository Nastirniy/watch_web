"""PKI at start-up: the settings are checked before the server runs, the CRL is published after the migrations
(no FastAPI here)."""
import os
from pathlib import Path

from app.backend.config import PkiConfig, settings
from app.backend.database import get_session_maker
from app.backend.pki.authority import CaCheckError, load_ca_certificate
from app.backend.pki.crl import split_pem_crls
from app.backend.services.crl_publisher import publish_crl
from app.backend.services.signer_client import SignerRefused
from app.backend.utils.logging_config import get_main_logger

_logger = get_main_logger()

_REQUIRED = ("ca_cert_path", "signer_socket", "store_dir", "crl_path")


class PkiSettingsError(RuntimeError):
    """settings.pki is partial or points at something unusable; the API refuses to start"""


def _writable_directory(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK)


def check_pki_settings(pki: PkiConfig) -> None:
    """Nothing set is fine; a partial configuration names the missing settings; a configured one must have a CA
    certificate, writable folders and parsable extra CRLs. The signer is not required to be up."""
    missing = [name for name in _REQUIRED if not getattr(pki, name)]
    if len(missing) == len(_REQUIRED):
        return
    if missing:
        raise PkiSettingsError(
            "PKI is partly configured: set MYSETTINGS_PKI__" + ", MYSETTINGS_PKI__".join(n.upper() for n in missing)
            + " as well (or none of them): missing " + ", ".join(missing))
    try:
        load_ca_certificate(pki.ca_cert_path)
    except CaCheckError as exc:
        raise PkiSettingsError(f"pki.ca_cert_path is not a usable CA certificate ({exc.reason})") from None
    if not _writable_directory(pki.store_dir):
        raise PkiSettingsError("pki.store_dir is not a writable directory")
    if not _writable_directory(str(Path(pki.crl_path).parent)):
        raise PkiSettingsError("the directory of pki.crl_path is not a writable directory")
    for path in pki.extra_crl_paths:
        try:
            data = Path(path).read_bytes()
            parsed = split_pem_crls(data)
        except (OSError, ValueError):
            raise PkiSettingsError(f"pki.extra_crl_paths: {path} is not a readable CRL file") from None
        if not parsed:
            raise PkiSettingsError(f"pki.extra_crl_paths: {path} holds no PEM CRL")


async def publish_crl_at_startup() -> None:
    """Publish the CRL when the file is missing or half of its validity has passed. The signer is not required for
    the API to start (spec section 3): a signer that does not answer or refuses (MYSETTINGS_PKI__CRL_DAYS above
    SIGNER_MAX_CRL_DAYS), an extra CRL that does not parse or a file that cannot be written is logged, the start goes
    on and /health shows the state of the CRL."""
    if not settings.pki.is_configured():
        return
    session_maker = await get_session_maker()
    async with session_maker() as session:
        try:
            published = await publish_crl(session, only_if_due=True)
        except SignerRefused as exc:
            _logger.error(f"CRL not published at start: the signer refused ({exc})")
            return
        except OSError as exc:
            _logger.error(f"CRL not published at start: {settings.pki.crl_path} cannot be written ({exc!r})")
            return
        if published:
            _logger.info("CRL published at start")
