"""nginx is the control point for client certificates: the files must keep it that way.

POST /auth/login trusts X-SSL-Client-* in production. They are safe only because nginx verifies the certificate
(ssl_verify_client) and sets those headers itself in every location that reaches the API, replacing whatever the
client sent. nginx copies proxy_set_header from the server level only into locations that set none of their
own, so each API location must include the proxy snippet.
"""
import inspect
import re
from pathlib import Path

import yaml

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
    """The locations a client reaches that proxy to the API (internal and named locations are reached only from
    another location)"""
    return [(header, body) for header, body in _blocks(APP, "location")
            if "proxy_pass http://watch_web_api" in body and not header.startswith("@") and "internal;" not in body]


def _matches(header: str, path: str) -> bool:
    """nginx prefix, exact and regular expression locations"""
    parts = header.split()
    if parts[0] == "=":
        return path == parts[1]
    if parts[0] in ("~", "~*"):
        return re.search(parts[1], path, re.IGNORECASE if parts[0] == "~*" else 0) is not None
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
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        assert any(_matches(header, path) for header in headers), f"no nginx location proxies {route.path} to the API"


def test_every_api_location_serves_a_route():
    """A location for a path the API no longer answers would hide that path from the web client"""
    paths = [re.sub(r"\{[^}]+\}", "1", route.path) for route in fastapi_app.routes]
    stale = [header for header, _ in _api_locations() if not any(_matches(header, path) for path in paths)]
    assert not stale, f"nginx locations without an API route: {stale}"


def test_production_server_requires_a_verified_client_certificate():
    servers = _blocks(SERVERS, "server")
    tls = [body for _, body in servers if "listen 443 ssl" in body]
    assert len(tls) == 1
    for directive in ("ssl_verify_depth 1;", "ssl_crl ", "/etc/letsencrypt/live/"):
        assert directive in tls[0], directive
    for directive in ("ssl_verify_client on;", "ssl_client_certificate ", "ssl_crl ", f"include {APP_SNIPPET};"):
        assert directive in tls[0], directive
    # every other server only redirects to it: no location is ever served without TLS
    for _, body in servers:
        if "listen 443 ssl" not in body:
            assert "return 301 https://" in body and "include" not in body and "location" not in body


def test_production_compose_does_not_prewarm_through_nginx():
    """Pre-warming asks nginx for a thumbnail without a client certificate, and nginx.conf serves nothing without
    one: port 80 only redirects, 443 refuses the request. docker-compose.yml pointed it at http://127.0.0.1:80, so
    every upload made a request that could only answer 301 (found on the test server, 2026-10-19)"""
    api = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["api"]["environment"]
    assert api.get("MYSETTINGS_STORAGE__THUMBNAIL__PREWARM") == "false"
    assert "MYSETTINGS_STORAGE__IMGPROXY__PREWARM_BASE_URL" not in api
    # the plain http e2e stack has a server pre-warming can reach, and keeps it on
    e2e = yaml.safe_load(E2E_COMPOSE)["services"]["api"]["environment"]
    assert e2e.get("MYSETTINGS_STORAGE__IMGPROXY__PREWARM_BASE_URL") == "http://nginx"
    assert e2e.get("MYSETTINGS_STORAGE__THUMBNAIL__PREWARM", "true") != "false"


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


def test_nginx_errors_keep_their_status():
    """error_page to /50x.html answered 404 on the host whenever the API was down (live crash test of 2026-10-19): the
    Ubuntu package has no 50x.html. The client reads only the status of an infrastructure error (API.md, "Errors").
    A handoff to a named location that proxies to the API is allowed: the API answers with its status."""
    named = {header: body for header, body in _blocks(APP, "location") if header.startswith("@")}
    for name, text in (("nginx-app.conf", APP), ("nginx.conf", SERVERS)):
        for target in re.findall(r"error_page [^;]*?(\S+);", _directives(text)):
            # the only error_page allowed hands the request to the API, which answers with its own status; inside
            # /_stream_auth a refusal becomes the 403 that sends the stream request to the API
            if target == "@stream_auth_refused":
                continue
            assert target in named and "proxy_pass http://watch_web_api" in named[target], f"{name}: error_page {target}"


# ------------------------------------------------------------------ /stream: authorised by the API, served by nginx

STREAM = r"~ ^/api/v1/media/\d+/stream$"


def _location(text, header):
    return next(body for h, body in _blocks(text, "location") if h == header)


def test_stream_is_authorised_by_the_api_and_served_by_nginx():
    body = _location(APP, STREAM)
    assert "auth_request /_stream_auth;" in body
    for header, var in (("x_accel_redirect", "stream_accel"), ("content_type", "stream_type"),
                        ("content_disposition", "stream_disposition"), ("x_file_hash", "stream_hash")):
        assert f"auth_request_set ${var} $upstream_http_{header};" in body
    assert "error_page 401 403 500 = @stream_api;" in body
    assert "try_files $stream_file @stream_api;" in body, "a missing file falls back to the API (media_file_missing)"
    assert "types { } default_type \"\";" in body and "add_header Content-Type $stream_type;" in body
    assert "etag off;" in body and "add_header X-Request-ID $request_id always;" in body
    root = re.search(r"\broot (\S+);", body).group(1)
    alias = re.search(r"\balias (\S+);", _location(APP, "^~ /protected_uploads/")).group(1)
    assert root.rstrip("/") + "/" == alias


def test_stream_authorisation_is_cached_30_s_per_token_certificate_and_uri():
    body = _location(APP, "= /_stream_auth")
    assert "internal;" in body and f"include {PROXY_SNIPPET};" in body
    assert "proxy_pass http://watch_web_api$request_uri;" in body
    assert 'proxy_cache_key "$http_authorization|$ssl_client_fingerprint|$request_uri";' in body
    assert re.findall(r"proxy_cache_valid [^;]+;", body) == ["proxy_cache_valid 200 30s;"]
    assert "proxy_cache stream_auth;" in body and "proxy_cache_lock on;" in body
    for header in ("Range", "If-None-Match", "If-Range", "Content-Length"):
        assert f'proxy_set_header {header} "";' in body
    assert "proxy_pass_request_body off;" in body
    assert "proxy_ignore_headers X-Accel-Redirect Cache-Control Expires Set-Cookie;" in body
    # auth_request copies the challenge of a 401 from the upstream answer itself (proxy_hide_header does not reach
    # it), then the fallback adds the API's: two WWW-Authenticate. A 401 is a plain 403 for auth_request instead,
    # and @stream_api answers the API's 401 with its one challenge.
    assert "proxy_intercept_errors on;" in body and "error_page 401 = @stream_auth_refused;" in body
    assert _location(APP, "@stream_auth_refused").strip() == "return 403;"


def test_stream_refusals_are_answered_by_the_api():
    body = _location(APP, "@stream_api")
    assert f"include {PROXY_SNIPPET};" in body and "proxy_pass http://watch_web_api;" in body


def test_stream_map_and_cache_zone_live_at_http_level_in_both_stacks():
    for name, text in (("nginx-http.conf", HTTP), ("nginx-http.test.conf", E2E_HTTP)):
        flat = _directives(text)
        assert "keys_zone=stream_auth:" in flat, name
        assert "map $stream_accel $stream_file { ~^/protected_uploads(?<stream_rest>/.+)$ $stream_rest; default \"\"; }" in flat, name


def test_no_prefix_location_takes_the_stream_path_from_the_regex():
    """A ^~ or = location matching the path would be chosen before any regex location"""
    for header, _ in _blocks(APP, "location"):
        parts = header.split()
        if parts[0] == "^~":
            assert not "/api/v1/media/1/stream".startswith(parts[1]), header
        if parts[0] == "=":
            assert parts[1] != "/api/v1/media/1/stream", header


def test_download_is_not_cached():
    assert "/download" not in _directives(APP)


def test_deployment_lists_every_media_root_line_to_edit():
    """DEPLOYMENT.md "Edit before reloading" names each line of nginx-app.conf that holds the media root: an operator
    who edits only some of them gets /stream asking the API twice per Range request, silently (final review)"""
    deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    lines = re.findall(r"^\s*((?:root|alias) /var/www/radio_monitoring/uploads/?;)", APP, re.M)
    assert len(lines) == 2, lines
    for line in lines:
        assert f"`{line}`" in deployment, line


def test_stream_answers_cannot_be_framed_by_another_site():
    """/protected_uploads/ answered X-Frame-Options SAMEORIGIN; the stream location serves the same files"""
    assert "add_header X-Frame-Options SAMEORIGIN;" in _location(APP, STREAM)
