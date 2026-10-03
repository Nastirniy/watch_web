"""nginx is the control point for client certificates: the files must keep it that way.

POST /auth/login trusts X-SSL-Client-* in production. They are safe only because nginx verifies the certificate
(ssl_verify_client) and sets those headers itself in every location that reaches the API, replacing whatever the
client sent. nginx copies proxy_set_header from the server level only into locations that set none of their
own, so each API location must include the proxy snippet.
"""
import inspect
import re
from pathlib import Path

from app.backend.api.v1.auth import login

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "nginx-app.conf").read_text(encoding="utf-8")
PROXY = (ROOT / "nginx-proxy.conf").read_text(encoding="utf-8")
SERVERS = (ROOT / "nginx.conf").read_text(encoding="utf-8")
E2E_SERVER = (ROOT / "tests/docker/nginx-server.test.conf").read_text(encoding="utf-8")
E2E_COMPOSE = (ROOT / "tests/docker/compose.test.yml").read_text(encoding="utf-8")

APP_SNIPPET = "/etc/nginx/snippets/watch-web-app.conf"
PROXY_SNIPPET = "/etc/nginx/snippets/watch-web-proxy.conf"


def _directives(text: str) -> str:
    """The configuration without comments, one space between words"""
    return re.sub(r"\s+", " ", re.sub(r"#[^\n]*", "", text))


def _blocks(text: str, keyword: str) -> list[tuple[str, str]]:
    """(header, body) of every `keyword ... { ... }` block, nested braces included"""
    text = _directives(text)
    blocks = []
    for match in re.finditer(rf"\b{keyword}\b([^{{;]*)\{{", text):
        depth, position = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[position], 0)
            position += 1
        blocks.append((match.group(1).strip(), text[match.end():position - 1]))
    return blocks


def _certificate_headers() -> set[str]:
    """The headers the login route reads: FastAPI turns x_ssl_client_dn into x-ssl-client-dn"""
    return {name.replace("_", "-") for name in inspect.signature(login).parameters if name.startswith("x_ssl_client_")}


def test_proxy_snippet_sets_every_certificate_header_from_nginx_variables():
    expected = {
        "x-ssl-client-verify": "$ssl_client_verify",
        "x-ssl-client-dn": "$ssl_client_s_dn",
        "x-ssl-client-serial": "$ssl_client_serial",
        "x-ssl-client-fingerprint": "$ssl_client_fingerprint",
    }
    assert set(expected) == _certificate_headers(), "the login route reads other headers than nginx sets"
    found = {name.lower(): value for name, value in re.findall(r"proxy_set_header (X-SSL-Client-\S+) (\S+);", _directives(PROXY))}
    assert found == expected


def test_every_api_location_includes_the_proxy_snippet():
    api_locations = [(header, body) for header, body in _blocks(APP, "location") if "proxy_pass http://watch_web_api" in body]
    assert {header for header, _ in api_locations} >= {"/", "/api/v1/media/nginx-upload", "^~ /api/v1/media/uploads"}
    for header, body in api_locations:
        assert f"include {PROXY_SNIPPET};" in body, f"location {header} reaches the API without the certificate headers"


def test_production_server_requires_a_verified_client_certificate():
    servers = _blocks(SERVERS, "server")
    tls = [body for _, body in servers if "listen 443 ssl" in body]
    assert len(tls) == 1
    for directive in ("ssl_verify_client on;", "ssl_client_certificate ", "ssl_crl ", f"include {APP_SNIPPET};"):
        assert directive in tls[0], directive
    # every other server only redirects to it: no location is ever served without TLS
    for _, body in servers:
        if "listen 443 ssl" not in body:
            assert "return 301 https://" in body and "include" not in body and "location" not in body


def test_e2e_stack_runs_the_production_locations():
    assert f"include {APP_SNIPPET};" in _directives(E2E_SERVER)
    assert f"../../nginx-app.conf:{APP_SNIPPET}:ro" in E2E_COMPOSE
    assert f"../../nginx-proxy.conf:{PROXY_SNIPPET}:ro" in E2E_COMPOSE
