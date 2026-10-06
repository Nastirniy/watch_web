# Deployment

## Topology

Docker runs the application only. Everything stateful stays outside the containers.

```
                     +----------------------- host --------------------------+
  clients  --443-->  |  nginx (entry point, TLS, upload offload, media,     |
                     |         thumbnail cache /var/cache/nginx/thumbs)      |
                     |     |  proxy 127.0.0.1:8000        |  /_thumb/,       |
                     |     |                              |  /media-images/  |
                     |     v                              v                  |
                     |  [api container]            [imgproxy container]      |
                     |  [media-worker container]                             |
                     |     |  127.0.0.1:3306                                 |
                     |     v                                                 |
                     |  MySQL (host service)                                 |
                     |                                                       |
                     |  MEDIA_ROOT  (local disk, or NFS/SMB share mounted    |
                     |               on the host) bind-mounted at the same   |
                     |               path into api, media-worker, imgproxy   |
                     +-------------------------------------------------------+
```

- Containers use the host network, so the database, nginx and imgproxy are all reached on `127.0.0.1` and nothing on the host needs to be reconfigured. A bridge-network variant is described at the bottom of `docker-compose.yml`.
- The media directory is mounted at the same absolute path inside and outside the containers. The database stores paths relative to it, so the directory can move as long as `MEDIA_ROOT` and the nginx `alias` move with it.
- The API applies migrations on start and seeds the admin user; the worker only needs the database and the media directory.

## Media storage

Any filesystem mounted on the host works: a local disk, an NFS export, an SMB share, or a block volume from the cloud. Mount it on the host, point `MEDIA_ROOT` at it, and give the mount to the containers through `docker-compose.yml` (already done with `${MEDIA_ROOT}:${MEDIA_ROOT}`).

What the pipeline needs from the filesystem:

| Need | Why |
|------|-----|
| POSIX rename on the same filesystem | uploads are moved from `MEDIA_ROOT/_tmp` into place; posters are written as `.tmp-xxxx` then renamed, storyboards as a `.tmp-` directory renamed into place |
| Readable by the nginx worker user | nginx serves originals with `alias`; imgproxy reads originals and posters |
| Writable by `MEDIA_UID:MEDIA_GID` | the API writes uploads, the worker writes `MEDIA_ROOT/_derived/posters/` and `MEDIA_ROOT/_derived/storyboards/` |
| Reasonable read latency | imgproxy reads the whole original on a cache miss; the nginx cache keeps that to once per thumbnail |

Notes for network shares:

- NFS: mount with `hard,intr` (or `hard` on modern kernels); keep the nginx cache on local disk, never on the share.
- SMB/CIFS: mount with `uid=MEDIA_UID,gid=MEDIA_GID,file_mode=0644,dir_mode=0755` so the containers and nginx agree on ownership. Rename is atomic on CIFS.
- Object storage (S3 and similar) is not a filesystem. Mount it with rclone or s3fs on the host if it must be used; native S3 support (imgproxy can read `s3://` sources, the API and nginx cannot yet) is a separate change.

Ownership: run `id -u <owner>` and `id -g <owner>` of `MEDIA_ROOT` and put the numbers in `MEDIA_UID` / `MEDIA_GID`. The containers run as that user. nginx only reads, so it needs group or world read (`chmod -R g+rX` or `o+rX`). The uid does not need an account inside the image: the image sets `USER`, `LOGNAME` and `HOME` so libraries that look up the current user keep working.

## Host nginx

Install the four files on the host:

```bash
sudo cp nginx-http.conf /etc/nginx/conf.d/00-watch-web-http.conf     # cache zone, upstreams (http context)
sudo mkdir -p /etc/nginx/snippets
sudo cp nginx-app.conf /etc/nginx/snippets/watch-web-app.conf         # locations (server context)
sudo cp nginx-proxy.conf /etc/nginx/snippets/watch-web-proxy.conf     # API proxy headers, certificate headers
sudo cp nginx.conf /etc/nginx/sites-available/watch_web               # server blocks: 80 -> 443, TLS + client certificates
sudo ln -s /etc/nginx/sites-available/watch_web /etc/nginx/sites-enabled/
sudo mkdir -p /var/www/nginx_uploads /var/cache/nginx/thumbs
sudo chown www-data:www-data /var/www/nginx_uploads /var/cache/nginx/thumbs
sudo nginx -t && sudo systemctl reload nginx
```

Edit before reloading:

| In file | Line | Set to |
|---------|------|--------|
| nginx-app.conf | `alias /var/www/radio_monitoring/uploads/;` | `MEDIA_ROOT` with a trailing slash |
| nginx-app.conf | `client_body_temp_path /var/www/nginx_uploads 1 2;` | `NGINX_UPLOAD_TEMP` |
| nginx.conf | `server_name` (both servers) | your domain |
| nginx.conf | `ssl_certificate` / `ssl_certificate_key` | the server certificate (with its chain) and key |
| nginx.conf | `ssl_client_certificate` / `ssl_crl` | the CA that issues user certificates and its revocation list |
| nginx.conf | `root /var/www/watch_web_client;` in `location /` | the web client build (`index.html` and assets) |
| nginx-http.conf | `server 127.0.0.1:8000;` / `server 127.0.0.1:8080;` | `API_PORT` / `IMGPROXY_PORT` |
| nginx-http.conf | `proxy_cache_path /var/cache/nginx/thumbs ... max_size=20g` | local disk, sized for the box |
| nginx-app.conf | `client_max_body_size 65m;` in the `/api/v1/media/uploads` location | above `storage.upload.max_chunk_bytes`, never equal: the API answers an oversized chunk itself (`upload_chunk_too_large`) |
| nginx-app.conf | `client_max_body_size 11G;` (server level) | above `storage.max_file_size_gb`, never equal: the API answers an oversized file itself (`media_file_too_large`) |

The API container mounts `NGINX_UPLOAD_TEMP` at the same path, so the temp file nginx names in `X-File-Name` is the file the API moves. The directory must be writable by nginx and readable by `MEDIA_UID`; the simplest arrangement is `MEDIA_UID` = the nginx worker user (`www-data`).

If another nginx or a load balancer sits in front of this host, keep this server block as is and pass TLS through to it (TCP, no TLS termination in front): this nginx must see the client certificate. The cache and the internal locations must live on the nginx that can read `MEDIA_ROOT`.

nginx answers some requests itself, in its own format: no or an invalid client certificate, a body above its limits, the API not reachable or too slow, a file it cannot serve. Those are the infrastructure's errors; clients handle them by status (API.md, "Errors"). Every response carries `X-Request-ID` (nginx's `$request_id`): the access log `/var/log/nginx/watch_web.access.log` (format `watch_web` in nginx-http.conf; rotated with the other `/var/log/nginx/*.log` by the distribution's logrotate) records it, the API logs it on every line of the request and returns it as the `error_id` of a 500. `grep <id>` in both logs (`docker compose logs api`) finds both sides of a request.

### Web client and other clients

- The web client is served by this server block at `/` (`location /` in `nginx.conf`, its `root` is deployment configuration); `nginx-app.conf` claims only the paths the API publishes (`/api/`, the OpenAPI document and viewers of each version included, `/problems`, `/health`). Same origin, so the API sends no CORS headers and needs none.
- Integrations that call the API from their servers (another system's backend, scripts) need no CORS either: they connect with a client certificate issued by the CA in `ssl_client_certificate`, like users.
- A browser application on another origin is the only case for CORS. Add it at this nginx, not in the API, so nginx's own responses (413, 502, 504) carry the headers too: an allowlist `map $http_origin $cors_origin` in the http context, `add_header Access-Control-Allow-Origin $cors_origin always`, `Access-Control-Allow-Credentials true` (the browser must present the client certificate), `Access-Control-Expose-Headers` for `Location, Retry-After, Upload-Offset, Upload-Length, Upload-Expires, X-Thumbnail-Status, Content-Disposition, Accept-Post, WWW-Authenticate, X-Request-ID`, and `Vary: Origin`. Browsers send the CORS preflight (`OPTIONS`) without a client certificate, which `ssl_verify_client on` refuses: such an origin needs `ssl_verify_client optional` with the certificate enforced per location for everything but `OPTIONS`. Design it with the first concrete integration.

### Client certificates (mTLS)

nginx is the control point for user certificates. `ssl_verify_client on` makes every request (API, thumbnails, `/api/v1/docs`) present a certificate issued by `ssl_client_certificate`, inside its validity period and not on `ssl_crl`; anything else gets an nginx 400 page ("No required SSL certificate was sent" / "The SSL certificate error") and never reaches the API. Port 80 only redirects to 443.

- The API sees the certificate only through four headers nginx sets in `nginx-proxy.conf`: `X-SSL-Client-Verify`, `-DN`, `-Serial`, `-Fingerprint`. `POST /auth/login` maps them to a user. nginx replaces any header of these names the client sends, but only in locations that include the snippet: every location with `proxy_pass http://watch_web_api` must include it (`tests/test_nginx_conf.py` fails otherwise).
- Revocation: a certificate added to the CRL is refused after `nginx -s reload`. Refresh the CRL before its `nextUpdate`: an expired CRL makes nginx refuse every certificate. A resumed TLS session is not checked again, for at most `ssl_session_timeout` (10 minutes). Sessions already logged in with the revoked certificate stay valid in the API until they end, but every request still needs the certificate here.
- `ssl_verify_depth 2` accepts a user certificate issued by an intermediate CA under the root in `ssl_client_certificate`; raise it for longer chains.
- Servers run with `MYSETTINGS_APP__ENVIRONMENT=production`. In `development` the login takes the certificate fields from a JSON body without any check.
- The admin user: set `MYSETTINGS_ADMIN__PKI_CERTIFICATE_CN` / `_SERIAL` / `_FINGERPRINT` from the real admin certificate (serial in hex, fingerprint SHA-1, as nginx reports them). Credentials generated on first start have no private key anyone can install; they only serve the development login.
- Check: `curl --cert user.crt --key user.key https://host/health` answers; the same without `--cert` gets 400.

## MySQL on the host

The containers connect to `MYSETTINGS_DATABASE__HOST=127.0.0.1` over the host network, so MySQL only needs to listen on TCP `127.0.0.1:3306` (the default) and the application user needs `'radio_user'@'127.0.0.1'` or `'radio_user'@'localhost'` grants over TCP. A socket-only MySQL (`skip-networking`) does not work from a container.

## Application containers

```bash
cp .env.example .env        # fill: database password, secret key, admin PKI, IMGPROXY_KEY/SALT, MEDIA_ROOT, MEDIA_UID/GID
docker compose build
docker compose up -d
docker compose ps           # api and imgproxy report healthy; media-worker has no health endpoint
docker compose logs -f media-worker
```

Key and salt: `python3 -c "import secrets; print(secrets.token_hex(32))"` twice. The same values go to `IMGPROXY_KEY` / `IMGPROXY_SALT` (imgproxy container) and `MYSETTINGS_STORAGE__IMGPROXY__KEY` / `SALT` (API). Rotating them invalidates every issued public thumbnail URL at once.

Air-gapped hosts: build on a connected machine, `docker save watch-web ghcr.io/imgproxy/imgproxy:v3 | gzip > images.tgz`, copy, `docker load`. Nothing is downloaded at runtime.

Upgrades: `docker compose build api && docker compose up -d api media-worker`. The API runs migrations on start; start the worker after the API has finished the first start. Upgrading to storyboards with `MYSETTINGS_STORAGE__POSTER__CONCURRENCY=1`: see the upgrade note under "Storyboards" (the worker refuses to start until the concurrency is 2 or storyboards are off).

Upgrading to sizes as displayed (vertical phone videos, EXIF-rotated photos): rows stored before keep the stored frame size until probed again. `docker compose exec media-worker python -m app.backend.media_tools probe-missing --all` probes every video and audio file again and re-reads every image size (headers only; `--limit N` per run, default 1000). Video uploads that failed only because they have no video stream become `unsupported` after `media_tools reconcile-posters --retry-failed`: the worker sees the probed metadata and stops trying.

## Storyboards (seek previews)

The media worker also builds a storyboard (sprite sheets, one ffmpeg pass over the keyframes with the last frame
repeated up to the end; a second, full-decode pass only when that writes nothing, within the same time limit) for
every video of at least `storage.storyboard.min_duration_seconds`, after its poster is ready. It is the heaviest job
the worker does (a long video can take minutes of one CPU core and reads the whole file), so it is limited:

- Upgrade note: storyboards are on by default and need `poster.concurrency` of at least 2. A worker configured with
  `MYSETTINGS_STORAGE__POSTER__CONCURRENCY=1` refuses to start after this upgrade: raise it to `2`, or set
  `MYSETTINGS_STORAGE__STORYBOARD__ENABLED=false`.
- `storyboard.slots` (default 1) storyboards run at once per worker process (several worker containers run N x
  `slots`), and the value must stay below `poster.concurrency`: the worker refuses to start otherwise. A poster is
  always claimed first, a storyboard only when no poster was claimed, so a new upload gets its poster as fast as
  without storyboards. Among storyboards the newest video goes first, so a backfill of old videos does not hold a
  new upload back.
- ffmpeg runs with `storyboard.threads` (default 1) threads and, on POSIX, `storyboard.nice` (default 10); it is
  killed after `storyboard.timeout_seconds` (default 1800). Failures retry after `backoff_seconds`, and the
  storyboard is `failed` after `max_attempts`.
- Stopping the worker (`docker compose stop media-worker`) cancels a running storyboard and gives the attempt back; a
  running poster is allowed to finish.
- At start the worker reconciles: videos without a row get one, ready rows whose files are missing are queued again,
  and `.tmp-` / `.old-` directories under `_derived/storyboards/` older than `timeout_seconds` are removed. A
  filesystem error there (e.g. a wrong owner of `_derived/storyboards/`) is logged and the worker goes on: posters
  never depend on storyboards. Every `upload.sweep_interval_seconds` the worker also queues videos that became
  eligible while it runs (e.g. a duration filled in by `media_tools probe-missing`).
- `storyboard.enabled = false` stops claiming and the API answers `unsupported`; existing files stay.
- Disk: a 10 x 10 sheet of 160 px tiles is roughly 100-200 KB; a two-hour recording (3 sheets) takes about 0.5 MB.
- The sheets are served like thumbnails: signed URLs under `/media-images/` (imgproxy, nginx cache); no nginx change.

```bash
docker compose exec media-worker python -m app.backend.media_tools storyboard-status
docker compose exec media-worker python -m app.backend.media_tools reconcile-storyboards                 # queue missing, repair missing files
docker compose exec media-worker python -m app.backend.media_tools reconcile-storyboards --retry-failed  # also re-queue failed ones
```

`storyboard-status` prints the counts per state (pending, processing, ready, failed) and the latest errors (media id, state, attempts, message). The worker heartbeat
details include `storyboards=<n>` running.

## End-to-end test stack

`tests/docker/compose.test.yml` runs the whole pipeline on a developer machine, including Docker Desktop on Windows: MySQL and nginx are containers there too (a bridge network, not the production layout), but the media path, the real locations and proxy headers (`nginx-app.conf`, `nginx-proxy.conf`) and the uid handling are the same as in production. Only the server block differs (`tests/docker/nginx-server.test.conf`: plain http, no client certificates, the API in development mode).

```bash
docker build -t watch-web .
docker compose -f tests/docker/compose.test.yml up -d
venv/Scripts/python tests/docker/e2e.py        # login, uploads, cache MISS/HIT, signed URLs, worker, Range
venv/Scripts/python tests/docker/formats.py    # every accepted image and video format, rotation, odd sizes, broken files
docker compose -f tests/docker/compose.test.yml down -v
```

The script exits non-zero when any check fails. Ports default to 18089 (nginx) and 18090 (API without nginx); Windows reserves large port ranges below 11000, override with `E2E_NGINX_PORT` / `E2E_DIRECT_PORT` if needed.

## Verification

```bash
# every request presents a user certificate (nginx refuses it otherwise)
CERT="--cert user.crt --key user.key"

# health through nginx
curl -s $CERT https://host/health | head -c 200

# originals: nginx must serve the body, the API only the redirect
curl -sI $CERT -H "Authorization: Bearer $TOKEN" https://host/api/v1/media/1/download | grep -i "x-served-by\|accept-ranges"

# thumbnails: first call MISS, second HIT
curl -sI $CERT -H "Authorization: Bearer $TOKEN" "https://host/api/v1/media/1/thumbnail?size=medium" | grep -i "x-cache\|content-type"

# public URL from a list response loads without a token (the certificate is still required)
curl -sI $CERT "https://host/media-images/<signature>/rs:fit:300:300:0:0/q:85/exp:.../<source>" | head -1

# queue and worker
docker compose exec media-worker python -m app.backend.media_tools poster-status
docker compose exec media-worker python -m app.backend.media_tools storyboard-status
```

`X-Cache: HIT` means nginx served from cache; `MISS` means imgproxy rendered. A 403 from `/media-images/` is an invalid or expired signature; a 422 from imgproxy usually means the source is missing under `MEDIA_ROOT` or exceeds `IMGPROXY_MAX_SRC_RESOLUTION`. Images above that limit get `thumbnail_status: unsupported` from the API (it reads the size from the header at upload), as long as the API has the same limit: docker-compose.yml passes `IMGPROXY_MAX_SRC_RESOLUTION` from `.env` (default 250 megapixels, enough for 200 MP phone cameras) to both containers. The limit bounds CPU time, not memory: libvips decodes a JPEG at a reduced scale and a PNG line by line, so a 200 MP source peaked at 63 MB (JPEG) and 124 MB (PNG) per render, and four 1600 px previews of a 200 MP PNG at once at 431 MB, under the 1 GB container limit. What it stops is a crafted file: a 274 KB PNG holding 1.6 gigapixels took 3.5 s of CPU and 305 MB per render, and the per-format file size limit cannot catch it.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| Thumbnail returns 502 through nginx | imgproxy container down, or `upstream imgproxy` points to the wrong port |
| Thumbnail returns 403 through nginx | key/salt mismatch between `.env` (`MYSETTINGS_STORAGE__IMGPROXY__*`) and the imgproxy container |
| imgproxy logs "permission denied" or 422 | `MEDIA_ROOT` not readable by `MEDIA_UID`, or mounted at a different path than `IMGPROXY_LOCAL_FILESYSTEM_ROOT` |
| Every request is `X-Cache: MISS` | cache directory not writable by nginx, or `nginx-http.conf` not loaded in the http context |
| Videos stay `pending` | media-worker container not running, or it cannot connect to MySQL on 127.0.0.1 |
| Uploads through nginx fail with "temp file not found" | `NGINX_UPLOAD_TEMP` differs from `client_body_temp_path`, or the directory is not mounted into the api container |
| API cannot connect to MySQL | MySQL not listening on TCP 127.0.0.1, or grants only for the socket user |
| Files written by containers unreadable by nginx | `MEDIA_UID`/`MEDIA_GID` do not match the owner of `MEDIA_ROOT`, or the share was mounted with restrictive `file_mode` |

## Without Docker

The same image contents run from a venv: `python -m uvicorn app.main:fastapi_app --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers` for the API, `systemd/watch-web-media-worker.service` for the worker, and imgproxy as a Podman or Docker container (it is distributed as an image only). Settings come from `.env`; `MYSETTINGS_STORAGE__UPLOAD_PATH` must equal `MEDIA_ROOT`.
