"""The media worker's sign of life for the container healthcheck (docker-compose.yml, RELEASE.md "Verify").

The worker touches ALIVE_PATH after every heartbeat it wrote to the database (and removes a file of an earlier run
at its start). `python -m app.backend.workers.alive` exits 0 while the file is younger than 3 heartbeat intervals
(storage.poster.heartbeat_seconds), 1 otherwise. No database call: the check costs nothing and a database outage
shows in the api's readiness, not here.
"""
import sys
import time
from pathlib import Path

from app.backend.config import settings

ALIVE_PATH = Path("/tmp/watch-web-worker.alive")


def touch() -> None:
    ALIVE_PATH.touch()


def clear() -> None:
    ALIVE_PATH.unlink(missing_ok=True)


def check() -> int:
    """0 while the file is fresh, 1 when it is stale or missing"""
    try:
        age = time.time() - ALIVE_PATH.stat().st_mtime
    except FileNotFoundError:
        return 1
    return 0 if age < 3 * settings.storage.poster.heartbeat_seconds else 1


if __name__ == "__main__":
    sys.exit(check())
