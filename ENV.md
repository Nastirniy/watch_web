# Environment Variables

All variables follow the pattern `MYSETTINGS_SECTION__KEY`.
They override the corresponding values in `app/config.toml`.

Variables marked `[REQUIRED]` have no default — the app will refuse to start if they are missing.
Variables marked `[REQUIRED in prod]` have an insecure or wrong default and must be set before going live.

---

## Required — app will not start without these

| Variable | Description |
|----------|-------------|
| `MYSETTINGS_DATABASE__PASSWORD` | MySQL password. No default. |
| `MYSETTINGS_SECURITY__SECRET_KEY` | JWT signing key. No default. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |

---

## Required in production — insecure or unusable defaults

| Variable | Default | Why it must change |
|----------|---------|-------------------|
| `MYSETTINGS_STORAGE__UPLOAD_PATH` | `uploads` (relative to CWD) | Must be an absolute path on the server, e.g. `/opt/app/uploads` |
| `MYSETTINGS_DATABASE__HOST` | `localhost` | Set to your database host |
| `MYSETTINGS_DATABASE__USER` | `dbuser` | Set to your database user |
| `MYSETTINGS_DATABASE__NAME` | `radio_monitoring` | Set to your database name |
| `MYSETTINGS_APP__ENVIRONMENT` | `production` | Must be `production` (disables debug output) |
| `MYSETTINGS_APP__DEBUG` | `false` | Must stay `false` in production |

---

## App

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_APP__NAME` | `Radio Monitoring System` | Application name shown in logs |
| `MYSETTINGS_APP__DEBUG` | `false` | Enable debug mode |
| `MYSETTINGS_APP__ENVIRONMENT` | `production` | `development` or `production` |
| `MYSETTINGS_APP__PROBLEM_BASE_URL` | empty | Base of the `type` URI of every error (RFC 9457), e.g. `https://monitoring.example/problems`. Empty: the request origin + `/problems`, served by the API itself |

---

## Database

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_DATABASE__HOST` | `localhost` | MySQL host |
| `MYSETTINGS_DATABASE__PORT` | `3306` | MySQL port |
| `MYSETTINGS_DATABASE__NAME` | `radio_monitoring` | Database name |
| `MYSETTINGS_DATABASE__USER` | `dbuser` | Database user |
| `MYSETTINGS_DATABASE__PASSWORD` | **[REQUIRED]** | Database password |
| `MYSETTINGS_DATABASE__POOL_SIZE` | `20` | Connection pool size |
| `MYSETTINGS_DATABASE__MAX_OVERFLOW` | `40` | Max extra connections above pool size |

---

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_SECURITY__SECRET_KEY` | **[REQUIRED]** | JWT signing key |
| `MYSETTINGS_SECURITY__ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token lifetime in minutes |
| `MYSETTINGS_SECURITY__REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime in days |
| `MYSETTINGS_SECURITY__SESSION_EXPIRE_HOURS` | `8` | Session lifetime in hours |
| `MYSETTINGS_SECURITY__ALLOWED_ORIGINS` | `["https://localhost:3000"]` | CORS allowed origins |
| `MYSETTINGS_SECURITY__JWT_LEEWAY_SECONDS` | `10` | Clock skew tolerance for JWT validation |

---

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_SERVER__HOST` | `127.0.0.1` | Bind address (use `0.0.0.0` behind a reverse proxy) |
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
| `MYSETTINGS_STORAGE__MAX_FILE_SIZE_GB` | `10` | Server ceiling for uploads in gigabytes (1-1024). The effective limit is per format (`media_formats.max_file_size_bytes`, managed through `/media-formats`) and cannot exceed this; nginx `client_max_body_size` must match it |
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
| `MYSETTINGS_STORAGE__IMGPROXY__PUBLIC_PREFIX` | `/t` | Public nginx location for signed, expiring thumbnail URLs. Must match nginx.conf |
| `MYSETTINGS_STORAGE__IMGPROXY__PUBLIC_BASE_URL` | *(empty)* | Origin prepended to public URLs, e.g. `https://monitoring.example`. Empty keeps them relative to the API host |
| `MYSETTINGS_STORAGE__IMGPROXY__PREWARM_BASE_URL` | *(empty)* | Origin the API uses to pre-warm the nginx cache after an upload, e.g. `http://nginx` in compose. Falls back to `PUBLIC_BASE_URL`; empty disables pre-warming |

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
| `MYSETTINGS_STORAGE__THUMBNAIL__PREWARM` | `true` | Request the medium thumbnail once after an image upload |

### Resumable uploads

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_STORAGE__UPLOAD__SESSION_TTL_HOURS` | `24` | Open sessions expire this long after creation; their part files are removed by the worker |
| `MYSETTINGS_STORAGE__UPLOAD__MAX_CHUNK_BYTES` | `67108864` | Largest accepted `PATCH` body (nginx `client_max_body_size` on the uploads location must allow it) |
| `MYSETTINGS_STORAGE__UPLOAD__RECOMMENDED_CHUNK_BYTES` | `8388608` | Chunk size suggested to clients |
| `MYSETTINGS_STORAGE__UPLOAD__SWEEP_INTERVAL_SECONDS` | `600` | How often the media worker expires stale sessions |

### Video posters (media worker)

Run the worker as its own process: `python -m app.backend.workers.media_worker` (compose service `media-worker`, or `systemd/watch-web-media-worker.service`).

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
| `MYSETTINGS_HEALTH__ENABLE_HOST_CHECK` | `true` | Restrict `/health` to allowed hosts |
| `MYSETTINGS_HEALTH__ALLOWED_HOSTS` | `["127.0.0.1", "localhost", "::1"]` | Hosts allowed to call `/health` |
| `MYSETTINGS_HEALTH__ENABLE_RATE_LIMIT` | `true` | Rate-limit `/health` requests |
| `MYSETTINGS_HEALTH__RATE_LIMIT_REQUESTS` | `10` | Max requests per window |
| `MYSETTINGS_HEALTH__RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |

---

## Admin account

| Variable | Default | Description |
|----------|---------|-------------|
| `MYSETTINGS_ADMIN__USERNAME` | `admin` | Default admin username (created on first seed) |
| `MYSETTINGS_ADMIN__USERNAME_ALIAS` | `System Administrator` | Display name for the admin account |
| `MYSETTINGS_ADMIN__PKI_CERTIFICATE_CN` | *(default)* | PKI certificate Common Name for admin PKI login |
| `MYSETTINGS_ADMIN__PKI_CERTIFICATE_SERIAL` | *(default)* | PKI certificate serial number |
| `MYSETTINGS_ADMIN__PKI_CERTIFICATE_FINGERPRINT` | *(default)* | PKI certificate fingerprint |

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
