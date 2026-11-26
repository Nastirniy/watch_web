# Application image for both the API and the media worker.
#
#   deploy/release.sh build                            (the release image watch-web:<tag>, RELEASE.md)
#   docker build -t watch-web:e2e .                    (the e2e stack, tests/docker/compose.test.yml)
#   API:    docker run watch-web:<tag>                 (default command)
#   worker: docker run watch-web:<tag> python -m app.backend.workers.media_worker
#
# imgproxy (thumbnail rendering) is a separate official image, see docker-compose.yml.

# The base image, pinned by the digest of its multi-platform index (python:3.13-slim on 2026-11-26). To move to a
# newer base: docker pull python:3.13-slim, then docker image inspect python:3.13-slim --format
# '{{index .RepoDigests 0}}', put the digest here and commit (RELEASE.md, "Release rules for developers").
ARG PYTHON_BASE_IMAGE=python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: video poster extraction (media worker)
# libmagic1: MIME detection on upload (python-magic)
# mariadb-client: mariadb-dump and mariadb for the db-backup tool service (BACKUPS.md)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libmagic1 mariadb-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

COPY requirements.txt ./
# Windows-only packages (pywin32, python-magic-bin, win32_setctime) are skipped on Linux
RUN grep -v -i -E '^(pywin32|python-magic-bin|win32_setctime)==' requirements.txt > requirements.linux.txt     && pip install -r requirements.linux.txt

COPY app ./app
COPY alembic.ini ./

# Build info (RELEASE.md): deploy/release.sh build passes the release tag, the commit and the commit time, so two
# builds of one commit carry the same values. app/backend/build_info.py reads build_info.json (/health, the worker
# heartbeat, cli/schema_check); packages.txt lists the Debian packages so two builds can be compared. A build without
# the args (docker build -t watch-web:e2e .) reports version dev.
ARG WATCH_WEB_VERSION=dev
ARG WATCH_WEB_REVISION=
ARG WATCH_WEB_CREATED=
LABEL org.opencontainers.image.title="watch-web"       org.opencontainers.image.version="${WATCH_WEB_VERSION}"       org.opencontainers.image.revision="${WATCH_WEB_REVISION}"       org.opencontainers.image.created="${WATCH_WEB_CREATED}"
RUN printf '{"version": "%s", "revision": "%s", "created": "%s"}
'         "$WATCH_WEB_VERSION" "$WATCH_WEB_REVISION" "$WATCH_WEB_CREATED" > /srv/app/app/build_info.json     && dpkg-query -W > /srv/app/app/packages.txt

# Media, temp spool and the nginx upload directory are bind-mounted from the host at runtime
# (docker-compose.yml, same path inside and outside). The container runs as the owner of that
# directory (MEDIA_UID/MEDIA_GID in .env); uid 10001 is only the fallback.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin watchweb \
    && mkdir -p /srv/uploads /srv/temp \
    && chown -R watchweb:watchweb /srv/app /srv/uploads /srv/temp \
    && chmod -R a+rX /srv/app
USER watchweb

# The container may run as any uid (MEDIA_UID, no /etc/passwd entry); libraries that call
# getpass.getuser() (aiomysql) need these set, and HOME must be writable for temp files.
ENV USER=watchweb \
    LOGNAME=watchweb \
    HOME=/tmp

# Defaults; docker-compose.yml overrides them with the host paths from .env.
# The image runs in production (config.toml, used by local runs, is development): the app refuses to start there
# without a real MYSETTINGS_SECURITY__SECRET_KEY. uvicorn trusts X-Forwarded-* only from FORWARDED_ALLOW_IPS
# (127.0.0.1 when unset), which is where host nginx connects from.
ENV MYSETTINGS_APP__ENVIRONMENT=production \
    MYSETTINGS_STORAGE__UPLOAD_PATH=/srv/uploads \
    MYSETTINGS_STORAGE__TEMP_PATH=/srv/temp \
    MYSETTINGS_SERVER__HOST=0.0.0.0 \
    MYSETTINGS_SERVER__PORT=8000 \
    MYSETTINGS_SERVER__WORKERS=4

EXPOSE 8000
# Shell form so host, port and worker count follow the environment (docker-compose.yml binds 127.0.0.1).
# The loop is uvloop (requirements.txt, picked by --loop auto): with --workers uvicorn binds the socket itself and
# asyncio then leaves TCP_NODELAY off on accepted sockets, so every response waited for a delayed ACK (about 40 ms).
# --timeout-graceful-shutdown: a stop waits at most 25 s for open requests (stop_grace_period in docker-compose.yml
# is 30 s); without it uvicorn waits for them for ever and Docker kills the process.
CMD ["sh", "-c", "exec python -m uvicorn app.main:fastapi_app --host \"$MYSETTINGS_SERVER__HOST\" --port \"$MYSETTINGS_SERVER__PORT\" --workers \"$MYSETTINGS_SERVER__WORKERS\" --proxy-headers --timeout-graceful-shutdown 25"]
