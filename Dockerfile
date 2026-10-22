# Application image for both the API and the media worker.
#
#   docker build -t watch-web .
#   API:    docker run watch-web                       (default command)
#   worker: docker run watch-web python -m app.backend.workers.media_worker
#
# imgproxy (thumbnail rendering) is a separate official image, see docker-compose.yml.
FROM python:3.13-slim

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
# Shell form so host, port and worker count follow the environment (docker-compose.yml binds 127.0.0.1)
CMD ["sh", "-c", "exec python -m uvicorn app.main:fastapi_app --host \"$MYSETTINGS_SERVER__HOST\" --port \"$MYSETTINGS_SERVER__PORT\" --workers \"$MYSETTINGS_SERVER__WORKERS\" --proxy-headers"]
