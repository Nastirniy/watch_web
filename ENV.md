# Environment Variables

All variables follow the pattern `MYSETTINGS_SECTION__KEY`.
They override the corresponding values in `app/config.toml`.

Variables marked `[REQUIRED]` have no default — the app will refuse to start if they are missing.
A variable or `config.toml` key that no setting declares (removed, renamed or misspelled) is ignored, and the app
says so at startup: `WARNING: unknown setting security.session_expire_hours is ignored`.
Variables marked `[REQUIRED in prod]` have an insecure or wrong default and must be set before going live.

---

## Required — app will not start without these

| Variable | Description |
|----------|-------------|
| `MYSETTINGS_DATABASE__PASSWORD` | MySQL password. No default. |
| `MYSETTINGS_SECURITY__SECRET_KEY` | JWT signing key. No default; in production an empty value or a placeholder (`your-secret-key-change-this-in-production`, the one in `app/config.toml`, and `CHANGE_ME`, the one in `.env.example`) stops the start. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |

---

## Required in production — insecure or unusable defaults

| Variable | Default | Why it must change |
|----------|---------|-------------------|
| `MYSETTINGS_STORAGE__UPLOAD_PATH` | `uploads` (relative to CWD) | Must be an absolute path on the server, e.g. `/opt/app/uploads` |
| `MYSETTINGS_DATABASE__HOST` | `localhost` | Set to your database host |
| `MYSETTINGS_DATABASE__USER` | `dbuser` | Set to your database user |
| `MYSETTINGS_DATABASE__NAME` | `radio_monitoring` | Set to your database name |
| `MYSETTINGS_APP__ENVIRONMENT` | `production` | Must be `production` (disables debug output). The Docker image sets it; the committed `app/config.toml` (local runs) says `development`, so a venv deployment must set it |
| `MYSETTINGS_APP__DEBUG` | `false` | Must stay `false` in production |

---

## App

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_APP__NAME` | `Radio Monitoring System` | Application name shown in logs |
| `MYSETTINGS_APP__DEBUG` | `false` | Enable debug mode |
| `MYSETTINGS_APP__ENVIRONMENT` | `production` in the code and the Docker image, `development` in `app/config.toml` | `development` or `production`. Outside production a warning line is logged at start; in production a placeholder secret stops the start |
| `MYSETTINGS_APP__PROBLEM_BASE_URL` | empty | Base of the `type` URI of every error (RFC 9457), e.g. `https://monitoring.example/problems`. Empty: the request origin + `/problems`, served by the API itself |

---

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_DATABASE__HOST` | `localhost` | MySQL host |
| `MYSETTINGS_DATABASE__PORT` | `3306` | MySQL port |
| `MYSETTINGS_DATABASE__NAME` | `radio_monitoring` | Database name |
| `MYSETTINGS_DATABASE__USER` | `dbuser` | Database user |
| `MYSETTINGS_DATABASE__PASSWORD` | **[REQUIRED]** | Database password. Latin-1 characters only (ASCII is safest): the MySQL driver (PyMySQL, also under aiomysql) sends it latin-1 encoded, a Cyrillic password cannot log in. Quotes, backslash, `#`, `;`, spaces, `$` and backticks work (checked live with the backup tools) |
| `MYSETTINGS_DATABASE__POOL_SIZE` | `10` | Connection pool size, per process |
| `MYSETTINGS_DATABASE__POOL_TIMEOUT_SECONDS` | `10` | How long a request waits for a free connection of the pool (at least 1); then it answers 503 `database_busy` with `Retry-After`. It was 30 s and a 500 before: under a refetch storm requests hung for 30 s |
| `MYSETTINGS_DATABASE__MAX_OVERFLOW` | `10` | Max extra connections above pool size, per process. Every api worker and the media worker is a process with its own pool: (`POOL_SIZE` + `MAX_OVERFLOW`) x (`MYSETTINGS_SERVER__WORKERS` + 1) must stay below the server's `max_connections` (151 by default) with room for the backup and admin tools. The defaults give 4 x 20 + 20 = 100 |

---

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_SECURITY__SECRET_KEY` | **[REQUIRED]** | JWT signing key; production refuses an empty or placeholder value |
| `MYSETTINGS_SECURITY__ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token lifetime in minutes, never beyond the hard end of its session |
| `MYSETTINGS_SECURITY__SESSION_IDLE_TIMEOUT_MINUTES` | `30` | A session ends after this long without a request; every request and refresh moves the end |
| `MYSETTINGS_SECURITY__SESSION_MAX_HOURS` | `0` | Optional hard limit since the login, whatever the activity. `0`: an active session never ends by itself |
| `MYSETTINGS_SECURITY__JWT_LEEWAY_SECONDS` | `10` | Clock skew tolerance for JWT validation |
| `MYSETTINGS_SECURITY__REFRESH_REUSE_GRACE_SECONDS` | `30` | The refresh token a refresh replaced, presented again this soon after it, is refused without ending the session (two tabs refreshing at once); any other reuse of a used refresh token ends the session (`refresh_token_reused`). `0`: every reuse ends it |

---

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_SERVER__HOST` | `127.0.0.1` | Bind address. Keep `127.0.0.1`: host nginx connects from the same machine, and with host networking (`docker-compose.yml`) `0.0.0.0` would open the port to the network, where anyone could send `X-SSL-Client-*` headers nginx never saw. Never `0.0.0.0` behind a reverse proxy on the same host. The Docker image itself defaults to `0.0.0.0` for bridge networks; `docker-compose.yml` overrides it |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Not a `MYSETTINGS_` variable: uvicorn reads it. Addresses whose `X-Forwarded-For` / `X-Forwarded-Proto` are trusted (uvicorn 0.35 default when `--forwarded-allow-ips` is not passed). The image no longer passes `*`. Set it only when nginx connects from another address (a bridge network); never `*` where the port is reachable by clients |
| `MYSETTINGS_SERVER__PORT` | `8000` | Bind port |
| `MYSETTINGS_SERVER__WORKERS` | `4` | Number of Uvicorn workers |
| `MYSETTINGS_SERVER__SSL_KEYFILE` | *(none)* | Path to SSL private key |
| `MYSETTINGS_SERVER__SSL_CERTFILE` | *(none)* | Path to SSL certificate |

---

## Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__UPLOAD_PATH` | `uploads` | **[REQUIRED in prod]** Absolute path to media storage root. Relative paths in the DB are resolved against this value. Video posters live under `<upload_path>/_derived/posters/`. |
| `MYSETTINGS_STORAGE__TEMP_PATH` | `temp` | Spool directory for multipart uploads |
| `MYSETTINGS_STORAGE__MAX_FILE_SIZE_GB` | `10` | Server ceiling for uploads in gigabytes (1-1024). The effective limit is per format (`media_formats.max_file_size_bytes`, managed through `/media-formats`) and cannot exceed this; nginx `client_max_body_size` stays above it (11G for 10), so the API answers an oversized file itself |
| `MYSETTINGS_STORAGE__FFMPEG_PATH` | `ffmpeg` | ffmpeg binary for the media worker (name on PATH or absolute path; falls back to the `imageio-ffmpeg` package) |
| `MYSETTINGS_STORAGE__FFPROBE_PATH` | `ffprobe` | ffprobe binary; when missing, durations are read from ffmpeg output |

### Nginx offloading (production)

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__ENABLE_NGINX` | `false` | Master switch: Nginx upload offloading, X-Accel-Redirect for downloads and streaming, X-Accel-Redirect to imgproxy for thumbnails. `docker-compose.yml` sets it to `true`; nginx itself runs on the host (`DEPLOYMENT.md`). |
| `MYSETTINGS_STORAGE__NGINX_UPLOAD_TEMP_PATH` | `/var/www/nginx_uploads` | Directory where Nginx writes uploaded files. Must match `client_body_temp_path` in nginx.conf and be reachable by the API under the same path |
| `MYSETTINGS_STORAGE__NGINX_INTERNAL_UPLOADS_PREFIX` | `/protected_uploads` | Nginx internal location for originals. Must match nginx.conf |

### imgproxy (thumbnail renderer)

Thumbnails and previews are rendered on demand by [imgproxy](https://imgproxy.net) from the original file (images) or from the poster frame (videos), and cached by nginx. Nothing is precomputed for images.

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__IMGPROXY__URL` | `http://127.0.0.1:8080` | imgproxy base URL, used when `ENABLE_NGINX=false` (the API proxies the render) |
| `MYSETTINGS_STORAGE__IMGPROXY__KEY` | *(empty)* | **[REQUIRED in prod]** Hex signing key. Must equal `IMGPROXY_KEY`. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `MYSETTINGS_STORAGE__IMGPROXY__SALT` | *(empty)* | **[REQUIRED in prod]** Hex signing salt. Must equal `IMGPROXY_SALT`. Empty key and salt means unsigned URLs, which imgproxy only accepts when it runs without a key (development) |
| `MYSETTINGS_STORAGE__IMGPROXY__NGINX_INTERNAL_PREFIX` | `/_thumb` | Internal nginx location used through X-Accel-Redirect. Must match nginx.conf |
| `MYSETTINGS_STORAGE__IMGPROXY__PUBLIC_PREFIX` | `/media-images` | Public nginx location for signed, expiring images made from media (thumbnails, previews). Must match the location in nginx-app.conf |
| `MYSETTINGS_STORAGE__IMGPROXY__PUBLIC_BASE_URL` | *(empty)* | Origin prepended to public URLs, e.g. `https://monitoring.example`. Empty keeps them relative to the API host, except the storyboard sheet URLs, which are always absolute: they then take the request origin as nginx passes it (`Host $host`, without a port), so set this when the site is not on the default port |
| `MYSETTINGS_STORAGE__IMGPROXY__PREWARM_BASE_URL` | *(empty)* | Origin the API uses to pre-warm the nginx cache after an upload: an nginx that serves `/media-images/` without a client certificate, e.g. `http://nginx` in the e2e stack. Falls back to `PUBLIC_BASE_URL`; both empty disables pre-warming |
| `MYSETTINGS_STORAGE__IMGPROXY__MAX_SRC_RESOLUTION_MP` | `250` | imgproxy's `IMGPROXY_MAX_SRC_RESOLUTION` (megapixels; 250 covers 200 MP phone cameras). A larger image is stored, but imgproxy refuses every render of it, so the API reports `thumbnail_status: unsupported` (`image_too_large_for_thumbnails`). docker-compose.yml sets both from `IMGPROXY_MAX_SRC_RESOLUTION` in `.env`; keep them equal anywhere else |

### Thumbnails

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__THUMBNAIL__SMALL_SIZE` | `[150, 150]` | Bounding box for `size=small` |
| `MYSETTINGS_STORAGE__THUMBNAIL__MEDIUM_SIZE` | `[300, 300]` | Bounding box for `size=medium` (default) |
| `MYSETTINGS_STORAGE__THUMBNAIL__LARGE_SIZE` | `[600, 600]` | Bounding box for `size=large` |
| `MYSETTINGS_STORAGE__THUMBNAIL__PREVIEW_SIZE` | `[1600, 1600]` | Bounding box for `size=preview` (lightbox) |
| `MYSETTINGS_STORAGE__THUMBNAIL__QUALITY` | `85` | Output quality (1-100) |
| `MYSETTINGS_STORAGE__THUMBNAIL__URL_TTL_SECONDS` | `3600` | Lifetime of the signed `thumbnail_urls` in list responses |
| `MYSETTINGS_STORAGE__THUMBNAIL__SHARE_MAX_TTL_SECONDS` | `604800` | Upper bound for `POST /media/{id}/share` |
| `MYSETTINGS_STORAGE__THUMBNAIL__PREWARM` | `true` | Request the medium thumbnail once after an image upload. `docker-compose.yml` sets `false`: behind `nginx.conf` every request needs a client certificate, so the API cannot reach the cache |

### Resumable uploads

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__UPLOAD__SESSION_TTL_HOURS` | `24` | Open sessions expire this long after creation; their part files are removed by the worker |
| `MYSETTINGS_STORAGE__UPLOAD__MAX_CHUNK_BYTES` | `67108864` | Largest accepted `PATCH` body (nginx `client_max_body_size` on the uploads location stays above it, 65m for 64 MB) |
| `MYSETTINGS_STORAGE__UPLOAD__RECOMMENDED_CHUNK_BYTES` | `8388608` | Chunk size suggested to clients |
| `MYSETTINGS_STORAGE__UPLOAD__SWEEP_INTERVAL_SECONDS` | `600` | How often the media worker expires stale sessions |

### Queued moves

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_MOVES__UPLOAD_STALE_SECONDS` | `300` | An upload that received no bytes for this long no longer keeps a move waiting; it is left alone and may go on after the move (minimum 60) |
| `MYSETTINGS_MOVES__CHECK_INTERVAL_SECONDS` | `15` | How often the media worker looks at the waiting moves (a backup: requests that meet a move look too); also the `Retry-After` of the answers 202 and 409 `move_pending` |

### Real-time events

Set on `api` and `media-worker` alike (compose: `EVENTS_ENABLED` in `.env`). The contract is API.md, "Real-time events".

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_EVENTS__ENABLED` | `false` | Off: nothing is published, no hub runs and `GET /events/stream` answers 204 (clients stop reconnecting); the media worker does not clean `outbox_events` |
| `MYSETTINGS_EVENTS__SERVE_STREAMS` | `true` | `false`: this process publishes but runs no hub and answers the stream with 204; the streams are served by a separate process (`--workers 1`, nginx upstream `watch_web_events`) |
| `MYSETTINGS_EVENTS__POLL_SECONDS` | `1.0` | How often each worker reads `outbox_events` (10% jitter) |
| `MYSETTINGS_EVENTS__GAP_TTL_SECONDS` | `60` | An id skipped by a late commit is watched this long, then counts as a rollback; keep it above `innodb_lock_wait_timeout` (50) |
| `MYSETTINGS_EVENTS__POSITION_HOLD_SECONDS` | `8` | A gap keeps the resume position below it this long at most |
| `MYSETTINGS_EVENTS__MAX_GAP_JUMP` | `1000` | A jump in ids above this is no gap (a restore raised `AUTO_INCREMENT`) |
| `MYSETTINGS_EVENTS__MAX_OPEN_GAPS` | `5000` | Gaps watched at most; the oldest go first |
| `MYSETTINGS_EVENTS__RING_ROWS` | `10000` | Events each worker keeps in memory for replay after a reconnect (about 5 MB) |
| `MYSETTINGS_EVENTS__QUEUE_MAX` | `256` | Messages waiting per stream; above it the stream closes (a client that stopped reading) |
| `MYSETTINGS_EVENTS__REPLAY_MAX` | `240` | A longer replay is `reload`; must stay below `QUEUE_MAX` |
| `MYSETTINGS_EVENTS__HEARTBEAT_SECONDS` | `20` | Heartbeat interval per stream; nginx `proxy_read_timeout` of the events location stays above three of them |
| `MYSETTINGS_EVENTS__SESSION_CHECK_SECONDS` | `10` | How often each worker checks the sessions of its streams (logout, idle end, revocation, a revoked certificate) |
| `MYSETTINGS_EVENTS__RIGHTS_REFRESH_SECONDS` | `30` | How often each worker reloads the scope tree and the grants of its connected users |
| `MYSETTINGS_EVENTS__RETENTION_SECONDS` | `3600` | Rows of `outbox_events` older than this are deleted by the media worker (minimum 60) |
| `MYSETTINGS_EVENTS__MAX_STREAMS_PER_WORKER` | `3000` | Above it a worker answers 503 `event_stream_capacity`; above the whole target, because streams do not spread evenly |
| `MYSETTINGS_EVENTS__REFETCH_RATE_PER_SECOND` | `30` | What one event may ask of the API: `refetch_within_ms` = subscribers x workers x 1000 / this |
| `MYSETTINGS_EVENTS__REFETCH_WINDOW_MAX_MS` | `60000` | Upper bound of `refetch_within_ms` |
| `MYSETTINGS_EVENTS__RETRY_MIN_MS` | `3000` | `retry:` at the start of a stream is random between this and `RETRY_MAX_MS` |
| `MYSETTINGS_EVENTS__RETRY_MAX_MS` | `15000` | See `RETRY_MIN_MS` |
| `MYSETTINGS_EVENTS__DRAIN_RETRY_MAX_MS` | `60000` | A `close` `restart` at shutdown carries a `retry` random between 1000 and this |

The hub of each worker has its own database engine (pool 1, overflow 1): budget 2 connections per api worker on top of `MYSETTINGS_DATABASE__POOL_SIZE` + `MAX_OVERFLOW`.

### Video posters (media worker)

Run the worker as its own process: `python -m app.backend.workers.media_worker` (compose service `media-worker`, or `deploy/systemd/watch-web-media-worker.service`).

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__POSTER__MAX_ATTEMPTS` | `3` | Failures before a video is marked `failed` |
| `MYSETTINGS_STORAGE__POSTER__LEASE_SECONDS` | `300` | Claim lease; a crashed worker's job becomes claimable again after this |
| `MYSETTINGS_STORAGE__POSTER__TIMEOUT_SECONDS` | `60` | ffmpeg is killed after this |
| `MYSETTINGS_STORAGE__POSTER__POLL_INTERVAL_SECONDS` | `1.0` | Queue poll interval while busy |
| `MYSETTINGS_STORAGE__POSTER__IDLE_POLL_INTERVAL_SECONDS` | `5.0` | Queue poll interval when idle |
| `MYSETTINGS_STORAGE__POSTER__FRAME_POSITION` | `0.1` | Fraction of the duration to seek to (0.0-1.0) |
| `MYSETTINGS_STORAGE__POSTER__MAX_SEEK_SECONDS` | `30` | Never seek deeper than this |
| `MYSETTINGS_STORAGE__POSTER__MAX_WIDTH` | `1920` | Poster width cap; every thumbnail size is made from it |
| `MYSETTINGS_STORAGE__POSTER__CONCURRENCY` | `2` | Parallel ffmpeg processes per worker |
| `MYSETTINGS_STORAGE__POSTER__HEARTBEAT_SECONDS` | `30` | Worker heartbeat interval |
| `MYSETTINGS_STORAGE__POSTER__STALE_AFTER_SECONDS` | `120` | `GET /media/posters/status` reports a worker stale after this |
| `MYSETTINGS_STORAGE__POSTER__BACKOFF_SECONDS` | `[30, 120, 480]` | Retry delays after the 1st, 2nd, ... failure |

### Video storyboards (media worker)

Seek previews (sprite sheets) made by the media worker with ffmpeg, only while the poster queue is empty.

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__STORYBOARD__ENABLED` | `true` | Off: no claims, the API answers `unsupported`, existing files stay |
| `MYSETTINGS_STORAGE__STORYBOARD__SLOTS` | `1` | Storyboards at once per worker process; must be below `MYSETTINGS_STORAGE__POSTER__CONCURRENCY` (the worker refuses to start otherwise: with a concurrency of 1, raise it to 2 or turn storyboards off) |
| `MYSETTINGS_STORAGE__STORYBOARD__MIN_DURATION_SECONDS` | `60` | Shorter videos get none |
| `MYSETTINGS_STORAGE__STORYBOARD__MAX_FRAMES` | `300` | Upper bound of frames per video; the step grows with the duration |
| `MYSETTINGS_STORAGE__STORYBOARD__MIN_STEP_SECONDS` | `2` | Lower bound of the frame step |
| `MYSETTINGS_STORAGE__STORYBOARD__TILE_WIDTH` | `160` | Width of one frame in pixels |
| `MYSETTINGS_STORAGE__STORYBOARD__COLUMNS` | `10` | Frames per row of a sheet |
| `MYSETTINGS_STORAGE__STORYBOARD__ROWS` | `10` | Rows per sheet |
| `MYSETTINGS_STORAGE__STORYBOARD__JPEG_QSCALE` | `5` | ffmpeg `-q:v` (2 best, 31 worst) |
| `MYSETTINGS_STORAGE__STORYBOARD__THREADS` | `1` | ffmpeg threads |
| `MYSETTINGS_STORAGE__STORYBOARD__NICE` | `10` | POSIX niceness of ffmpeg, 0 = unchanged |
| `MYSETTINGS_STORAGE__STORYBOARD__TIMEOUT_SECONDS` | `1800` | ffmpeg is killed after this |
| `MYSETTINGS_STORAGE__STORYBOARD__LEASE_SECONDS` | `300` | Claim lease, renewed while ffmpeg runs |
| `MYSETTINGS_STORAGE__STORYBOARD__MAX_ATTEMPTS` | `3` | Failures before a storyboard is marked `failed` |
| `MYSETTINGS_STORAGE__STORYBOARD__BACKOFF_SECONDS` | `[60, 300, 1800]` | Retry delays after the 1st, 2nd, ... failure |
| `MYSETTINGS_STORAGE__STORYBOARD__RETRY_AFTER_SECONDS` | `30` | `Retry-After` of the 202 answer of `GET /media/{id}/storyboard` |

---

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_LOGGING__LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MYSETTINGS_LOGGING__ENABLE_FILE_LOGGING` | `false` | Write logs to a file in addition to stdout |
| `MYSETTINGS_LOGGING__FILE_PATH` | *(none)* | Log file path, e.g. `/var/log/app/app.log` |

---

## Timezone

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_TIMEZONE__TIMEZONE` | `UTC` | IANA timezone name, e.g. `Europe/Kyiv`, `America/New_York` |
| `MYSETTINGS_TIMEZONE__DST_HANDLING` | `true` | Enable automatic daylight saving time adjustment |

---

## Health endpoint

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_HEALTH__ENABLE_HOST_CHECK` | `true` in production, `false` in development | Restrict `/health` to allowed client addresses. Unset: follows the environment; set it to decide explicitly |
| `MYSETTINGS_HEALTH__ALLOWED_HOSTS` | `["127.0.0.1", "localhost", "::1"]` | Client addresses allowed to call `/health` (`request.client.host`: nginx passes the real peer in `X-Forwarded-For`, so a remote client is refused; call it on the host) |
| `MYSETTINGS_HEALTH__ENABLE_RATE_LIMIT` | `true` | Rate-limit `/health` requests |
| `MYSETTINGS_HEALTH__RATE_LIMIT_REQUESTS` | `10` | Max requests per window |
| `MYSETTINGS_HEALTH__RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |

---

## Reports

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_REPORTS__MAX_ROWS` | `20000` | Most band rows in one Excel report (`POST /observations/report`); a larger result answers `422 report_too_large` with `max_rows`. The workbook is built in memory |

---

## Admin account

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_ADMIN__USERNAME` | `admin` | Default admin username (created on first seed) |
| `MYSETTINGS_ADMIN__USERNAME_ALIAS` | `System Administrator` | Display name for the admin account |
| `MYSETTINGS_ADMIN__PKI_CERTIFICATE_CN` | *(default)* | PKI certificate Common Name for admin PKI login (development seed only; on a server the first administrator gets a certificate from `python -m app.backend.cli.pki issue`, PKI.md) |
| `MYSETTINGS_ADMIN__PKI_CERTIFICATE_SERIAL` | *(default)* | PKI certificate serial number (development seed only) |
| `MYSETTINGS_ADMIN__PKI_CERTIFICATE_FINGERPRINT` | *(default)* | PKI certificate fingerprint (development seed only) |

---

## Client certificates (PKI)

Optional: PKI is configured when the four paths are all set, and the API refuses to start with only some of them,
or with one of the numbers below out of its range.
With Docker, `docker-compose.pki.yml` sets the four paths from `PKI_DIR`; do not set them in `.env`. Setup: `PKI.md`.

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_PKI__CA_CERT_PATH` | *(empty)* | The client CA certificate (PEM), read-only (`PKI_DIR/ca/client-ca.crt`) |
| `MYSETTINGS_PKI__SIGNER_SOCKET` | *(empty)* | The Unix socket of the `pki-signer` container (`PKI_DIR/run/signer.sock`) |
| `MYSETTINGS_PKI__STORE_DIR` | *(empty)* | Folder of every issued package, `<user_id>/<certificate_id>/` (`PKI_DIR/issued`, 0770 root:`PKI_SOCKET_GID`, never owned by `MEDIA_UID`) |
| `MYSETTINGS_PKI__CRL_PATH` | *(empty)* | The CRL file nginx reads (`ssl_crl`), written by the API (`PKI_DIR/crl/nginx.crl`) |
| `MYSETTINGS_PKI__EXTRA_CRL_PATHS` | `[]` | JSON list of CRL files appended to the published one: the existing root's CRL while its certificates live |
| `MYSETTINGS_PKI__VALIDITY_DAYS` | `365` | Default validity of a new certificate; 1 to `MAX_VALIDITY_DAYS` |
| `MYSETTINGS_PKI__MAX_VALIDITY_DAYS` | `730` | Longest validity a request may ask for (`validity_days_too_long` above); at most `SIGNER_MAX_VALIDITY_DAYS`, or the signer refuses (500 on issue) |
| `MYSETTINGS_PKI__KEY_SIZE` | `2048` | RSA key size of user keys, 2048 to 4096 |
| `MYSETTINGS_PKI__P12_ENCRYPTION` | `aes256` | `aes256` (AES-256 with SHA-256), or `legacy` (3DES with SHA-1) for old Windows and macOS |
| `MYSETTINGS_PKI__CRL_DAYS` | `7` | How long a signed CRL stays valid (`next_update`), at least 1; it is signed again daily. At most `SIGNER_MAX_CRL_DAYS`, or the signer refuses every CRL (logged; `/health` turns `degraded` as the published CRL ages) |

Compose variables of `.env` for the override: `COMPOSE_FILE=docker-compose.yml:docker-compose.pki.yml`, `PKI_DIR`
(host directory outside the project directory), `PKI_SIGNER_UID` (owner of `PKI_DIR/private`, runs the signer),
`PKI_SOCKET_GID` (a group of its own for the signer socket, which only the api container joins).

The signer never reads `.env`. Its settings are `SIGNER_*` in `signer.env` (from `signer.env.example`, 0600):
`SIGNER_CA_CERT_PATH`, `SIGNER_CA_KEY_PATH` (required), `SIGNER_CA_KEY_PASSWORD` (empty: unencrypted key),
`SIGNER_SOCKET_PATH` (required), `SIGNER_MAX_VALIDITY_DAYS` (`730`), `SIGNER_MAX_CRL_DAYS` (`31`).

## Database backups

Optional: backups are configured when `MYSETTINGS_BACKUP__DIR` is set; the API then refuses to start if the directory
(or the copy directory) cannot be read. With Docker, `docker-compose.backup.yml` sets `DIR` from `BACKUP_DIR` and
`docker-compose.backup-copy.yml` sets `COPY_DIR` from `BACKUP_COPY_DIR`; do not set them in `.env`. Guide: `BACKUPS.md`.

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_BACKUP__DIR` | *(empty)* | The backup folders, `<id>/dump.sql.gz` + `manifest.json` (`BACKUP_DIR`, 0750, `BACKUP_UID:BACKUP_GID`) |
| `MYSETTINGS_BACKUP__COPY_DIR` | *(empty)* | Off-host copy of every backup (a mounted share), same retention |
| `MYSETTINGS_BACKUP__STALE_AFTER_HOURS` | `26` | The newest good backup older than this is stale: `/health` degraded, `stale` in `GET /database/backups` |
| `MYSETTINGS_BACKUP__KEEP_DAILY` | `7` | Scheduled backups: the newest of each of the last N days |
| `MYSETTINGS_BACKUP__KEEP_WEEKLY` | `4` | Scheduled backups: the newest of each of the last N ISO weeks |
| `MYSETTINGS_BACKUP__KEEP_MONTHLY` | `6` | Scheduled backups: the newest of each of the last N months |
| `MYSETTINGS_BACKUP__KEEP_MANUAL_DAYS` | `90` | `manual` and `pre-upgrade` backups are kept this many days; the newest backup is never deleted |
| `MYSETTINGS_BACKUP__KEEP_PREVIOUS_DAYS` | `7` | `<db>_before_*` schemas a restore kept for `undo-restore`; `prune` drops older ones |
| `MYSETTINGS_BACKUP__MIN_FREE_MB` | `512` | `create` refuses (`no_space`) when the free space is below 1.5 x the last backup plus this |
| `MYSETTINGS_BACKUP__DUMP_TIMEOUT_SECONDS` | `3600` | A dump or a load running longer is killed (`timeout`) |
| `MYSETTINGS_BACKUP__DATADIR` | *(empty)* | The MariaDB data directory as the tool sees it (compose: `DB_DATADIR`, default `/var/lib/mysql`, read-only). `restore` and `verify --deep` load a copy there and refuse (`no_space`) when it lacks 1.2 x the database plus `MIN_FREE_MB`; empty or unreadable: `space_unknown` unless `--skip-space-check` |

Compose variables of `.env`: `COMPOSE_FILE=docker-compose.yml:docker-compose.backup.yml` (plus
`:docker-compose.backup-copy.yml` with a share, plus `:docker-compose.pki.yml` with PKI), `BACKUP_DIR`,
`BACKUP_COPY_DIR`, `BACKUP_UID` / `BACKUP_GID` (a user and group of their own: the tool runs as them, the api joins
the group; never `MEDIA_UID` / `MEDIA_GID`, often the nginx user), `DB_DATADIR` (MariaDB's `@@datadir`, when it is
not `/var/lib/mysql`).

---

## Minimal production `.env` example

```bash
# /opt/app/.env
# Permissions: chmod 600, owned by the app user

MYSETTINGS_APP__ENVIRONMENT=production
MYSETTINGS_APP__DEBUG=false

MYSETTINGS_DATABASE__HOST=db.internal
MYSETTINGS_DATABASE__NAME=radio_monitoring
MYSETTINGS_DATABASE__USER=appuser
MYSETTINGS_DATABASE__PASSWORD=strong-db-password

MYSETTINGS_SECURITY__SECRET_KEY=replace-with-output-of-secrets-token-hex-32

MYSETTINGS_STORAGE__UPLOAD_PATH=/opt/app/uploads

MYSETTINGS_LOGGING__LEVEL=INFO
```
