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
from app.backend.config import settings
from app.backend.services.media.formats import hard_limit_bytes
from app.main import fastapi_app

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "nginx-app.conf").read_text(encoding="utf-8")
PROXY = (ROOT / "nginx-proxy.conf").read_text(encoding="utf-8")
HTTP = (ROOT / "nginx-http.conf").read_text(encoding="utf-8")
SERVERS = (ROOT / "nginx.conf").read_text(encoding="utf-8")
E2E_HTTP = (ROOT / "tests/docker/nginx-http.test.conf").read_text(encoding="utf-8")
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


def _api_locations() -> list[tuple[str, str]]:
    return [(header, body) for header, body in _blocks(APP, "location") if "proxy_pass http://watch_web_api" in body]


def _matches(header: str, path: str) -> bool:
    """nginx prefix and exact locations (the files use no regular expressions)"""
    parts = header.split()
    if parts[0] == "=":
        return path == parts[1]
    return path.startswith(parts[-1])


def test_every_api_location_includes_the_proxy_snippet():
    api_locations = _api_locations()
    assert {header for header, _ in api_locations} >= {"/api/", "/api/v1/media/nginx-upload", "^~ /api/v1/media/uploads"}
    for header, body in api_locations:
        assert f"include {PROXY_SNIPPET};" in body, f"location {header} reaches the API without the certificate headers"


def test_every_api_route_reaches_the_api():
    """The locations claim the API's paths only, so "/" stays free for the web client; none may be missed"""
    headers = [header for header, _ in _api_locations()]
    assert "/" not in headers, "/ belongs to the server block (the web client), not to the API"
    for route in fastapi_app.routes:
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        assert any(_matches(header, path) for header in headers), f"no nginx location proxies {route.path} to the API"


def test_every_api_location_serves_a_route():
    """A location for a path the API no longer answers would hide that path from the web client"""
    paths = [re.sub(r"\{[^}]+\}", "x", route.path) for route in fastapi_app.routes]
    stale = [header for header, _ in _api_locations() if not any(_matches(header, path) for path in paths)]
    assert not stale, f"nginx locations without an API route: {stale}"


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


# ------------------------------------------------------------------ the API decides, nginx is the safety net

def _without_blocks(text: str, keyword: str) -> str:
    """The configuration without its `keyword ... { ... }` blocks: what applies at the level of the file"""
    text = _directives(text)
    for header, body in _blocks(text, keyword):
        text = text.replace(f"{keyword} {header} {{{body}}}", "")
    return text


def _size(value: str) -> int:
    number, unit = re.fullmatch(r"(\d+)([kKmMgG]?)", value).groups()
    return int(number) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit.lower()]


def _body_limit(text: str) -> int:
    return _size(re.search(r"client_max_body_size (\S+);", text).group(1))


def test_nginx_body_limits_sit_above_the_api_limits():
    """Where nginx and the API check the same size, an equal limit let nginx answer first with its own HTML 413:
    the client never got the API's upload_chunk_too_large / media_file_too_large with their params"""
    assert _body_limit(_without_blocks(APP, "location")) > hard_limit_bytes()
    uploads = dict(_blocks(APP, "location"))["^~ /api/v1/media/uploads"]
    assert _body_limit(uploads) > settings.storage.upload.max_chunk_bytes


# ------------------------------------------------------------------ one request id from nginx to the API log

REQUEST_ID_ANSWER = "add_header X-Request-ID $request_id always;"


def test_nginx_hands_its_request_id_to_the_api():
    proxy = _directives(PROXY)
    assert "proxy_set_header X-Request-ID $request_id;" in proxy, "replaces whatever the client sent"
    assert "proxy_hide_header X-Request-ID;" in proxy, "one X-Request-ID in a response: the one nginx adds"


def test_every_response_of_nginx_carries_the_request_id():
    """nginx's own errors too (always): an HTML 413 or 502 belongs to the infrastructure, and its request id is
    what finds it in the access log"""
    assert REQUEST_ID_ANSWER in _without_blocks(APP, "location")
    for header, body in _blocks(APP, "location"):
        if "add_header" in body:   # a location with add_header of its own inherits none of the server's
            assert REQUEST_ID_ANSWER in body, f"location {header} answers without X-Request-ID"


def _imgproxy_locations() -> dict[str, str]:
    return {header: body for header, body in _blocks(APP, "location") if "proxy_pass http://imgproxy" in body}


def _public_thumbnails_location() -> str:
    return f"^~ {settings.storage.imgproxy.public_prefix.rstrip('/')}/"


def test_public_thumbnail_urls_have_their_location():
    """The API builds public URLs from settings.storage.imgproxy.public_prefix; nginx must serve exactly that path"""
    assert _public_thumbnails_location() in _imgproxy_locations()


def test_public_thumbnail_links_stop_working_at_their_expiry():
    """Public thumbnail URLs carry exp:, which only imgproxy checks, on a cache miss: a cached copy kept answering
    for 30 days after the expiry. imgproxy caps the max-age of such a response at exp:, so the cache follows that
    header and never serves a stale copy (a stale copy is an expired link)"""
    public = _imgproxy_locations()[_public_thumbnails_location()]
    ignored = re.search(r"proxy_ignore_headers ([^;]+);", public)
    assert ignored is None or "Cache-Control" not in ignored.group(1).split()
    for args in re.findall(r"proxy_cache_valid ([^;]+);", public):
        codes = args.split()[:-1]   # no code means 200 301 302
        assert codes and not {"200", "any"} & set(codes), f"proxy_cache_valid {args} outlives exp:"
    assert "proxy_cache_use_stale" not in public


def test_a_thumbnail_answer_carries_one_cache_control():
    """imgproxy's own Cache-Control (public, its max-age) came next to the one nginx adds: two conflicting headers"""
    for header, body in _imgproxy_locations().items():
        assert "proxy_hide_header Cache-Control;" in body, f"location {header} passes imgproxy's Cache-Control on"


def test_the_access_log_records_the_request_id():
    formats = [re.search(r"log_format watch_web (.*?);", _directives(text)) for text in (HTTP, E2E_HTTP)]
    assert all(found and "$request_id" in found.group(1) for found in formats)
    assert formats[0].group(1) == formats[1].group(1), "the e2e stack logs like production"
    assert re.search(r"access_log \S+ watch_web;", _without_blocks(APP, "location"))


def test_upload_temp_path_agrees_with_the_api_setting():
    """The API accepts X-File-Name only inside storage.nginx_upload_temp_path: nginx must write there"""
    declared = re.findall(r"client_body_temp_path\s+(\S+)", _directives(APP))
    assert declared == [settings.storage.nginx_upload_temp_path]
    assert f"{settings.storage.nginx_upload_temp_path}" in E2E_COMPOSE
    assert "NGINX_UPLOAD_TEMP" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_nginx_replaces_the_client_forwarded_for_with_the_peer_address():
    """$proxy_add_x_forwarded_for appends to what the client sent, and uvicorn takes the leftmost entry as the
    client: a forged X-Forwarded-For would become request.client.host"""
    proxy = _directives(PROXY)
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in proxy
    assert "$proxy_add_x_forwarded_for" not in proxy
