"""SIGTERM ends the streams at once, on both event loops: uvicorn runs lifespan shutdown only after every connection
ended, so the hub chains its own SIGTERM handler in front of uvicorn's (services/events/hub.py install_drain). A real
signal to a real uvicorn with 2 workers: this cannot be shown in-process. POSIX only (run it on Linux, the e2e
stack or the app image)."""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from tests.paths import PROJECT_ROOT
from app.backend.api.v1 import V1
from app.backend.enums.routes import EventRoutes

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals and uvicorn --workers")

ROOT = PROJECT_ROOT


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _connect(port: int, deadline: float) -> socket.socket:
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def _read_until(sock: socket.socket, marker: bytes, deadline: float) -> bytes:
    data = b""
    sock.settimeout(0.5)
    while marker not in data:
        if time.monotonic() > deadline:
            raise AssertionError(f"{marker!r} not received; got {data[-300:]!r}")
        try:
            part = sock.recv(65536)
        except socket.timeout:
            continue
        if not part:
            break
        data += part
    return data


@pytest.mark.parametrize("loop", ["asyncio", "uvloop"])
def test_sigterm_closes_every_stream_with_restart_and_the_server_exits(tmp_path, loop):
    if loop == "uvloop":
        pytest.importorskip("uvloop")
    port = _free_port()
    environment = {**os.environ, "WW_DRAIN_DB": str(tmp_path / "drain.db"), "PYTHONPATH": str(ROOT)}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tests.event_drain_app:app", "--host", "127.0.0.1", "--port", str(port),
         "--workers", "2", "--loop", loop, "--timeout-graceful-shutdown", "25", "--log-level", "warning"],
        cwd=ROOT, env=environment)
    try:
        streams = []
        deadline = time.monotonic() + 120     # each worker builds its SQLite schema first (slow on overlay file systems)
        for _ in range(4):
            sock = _connect(port, deadline)
            path = V1.path(EventRoutes.STREAM).encode()
            sock.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n\r\n")
            assert b"event: ready" in _read_until(sock, b"event: ready", deadline)
            streams.append(sock)
        stopped_at = time.monotonic()
        server.send_signal(signal.SIGTERM)
        for sock in streams:
            data = _read_until(sock, b'"reason":"restart"', time.monotonic() + 5)
            assert b"event: close" in data and b"retry: " in data
        server.wait(timeout=10)
        assert time.monotonic() - stopped_at < 5, "the stop waited for the streams"
    finally:
        if server.poll() is None:
            server.kill()
