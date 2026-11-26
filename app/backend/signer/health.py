"""The signer's container healthcheck: GET /health over its own Unix socket (docker-compose.pki.yml).

`python -m app.backend.signer.health` exits 0 when the signer answers 200, 1 otherwise (no socket, a refusal, a
timeout). It reads only SIGNER_SOCKET_PATH, like the signer itself (signer.env).
"""
import os
import sys

import httpx

from app.backend.enums.routes import SignerRoutes

TIMEOUT_SECONDS = 5


def _transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport(uds=os.environ.get("SIGNER_SOCKET_PATH", ""))


def main() -> int:
    try:
        with httpx.Client(transport=_transport(), base_url="http://signer", timeout=TIMEOUT_SECONDS) as client:
            response = client.get(SignerRoutes.HEALTH.value)
    except (httpx.HTTPError, OSError) as exc:
        print(f"signer health: no answer ({type(exc).__name__})", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"signer health: status {response.status_code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
