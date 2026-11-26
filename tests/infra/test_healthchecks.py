"""Health checks of the compose services (RELEASE.md, "Verify"; deploy/release.sh waits on them).

The compose files are shared by every release that runs on them: after a rollback an image from before these
checks runs under the same healthcheck. So the worker and signer checks run a module only when the image has it
(else they pass, as no check did before), and never the worker's or signer's own main.
"""
import os
import subprocess
import sys
import time

import pytest
import yaml

from app.backend.config import settings
from app.backend.workers import alive
from tests.paths import PROJECT_ROOT


def services(name: str) -> dict:
    return yaml.safe_load((PROJECT_ROOT / name).read_text(encoding="utf-8"))["services"]


# ---------------------------------------------------------------- the worker's alive file

@pytest.fixture
def alive_file(tmp_path, monkeypatch):
    path = tmp_path / "watch-web-worker.alive"
    monkeypatch.setattr(alive, "ALIVE_PATH", path)
    return path


def test_a_fresh_file_is_alive(alive_file):
    alive.touch()
    assert alive.check() == 0


def test_a_stale_file_is_not(alive_file):
    alive.touch()
    old = time.time() - 3 * settings.storage.poster.heartbeat_seconds - 5
    os.utime(alive_file, (old, old))
    assert alive.check() == 1


def test_a_missing_file_is_not(alive_file):
    assert alive.check() == 1


def test_clear_removes_a_file_of_an_earlier_run(alive_file):
    alive.touch()
    alive.clear()
    assert not alive_file.exists()
    alive.clear()   # nothing to remove: no error


async def test_the_worker_touches_the_file_after_a_heartbeat(alive_file, session_maker, db, tmp_path):
    from app.backend.workers.media_worker import MediaWorker
    worker = MediaWorker(session_maker, tmp_path, name="media@test:1", concurrency=4, extractor=lambda s, t: None)
    await worker.write_heartbeat()
    assert alive_file.exists() and alive.check() == 0


def test_the_module_check_runs_without_a_database():
    result = subprocess.run([sys.executable, "-m", "app.backend.workers.alive"], cwd=PROJECT_ROOT,
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)})
    assert result.returncode in (0, 1)


# ---------------------------------------------------------------- compose

GUARD = "importlib.util.find_spec"


def test_the_api_waits_on_readiness_with_a_fallback_for_older_images():
    check = services("docker-compose.yml")["api"]["healthcheck"]
    command = check["test"][-1]
    assert "/health/ready" in command and "/health'" in command, "404 on an older image falls back to /health"
    assert check["start_period"] == "20m"
    assert check["interval"] == "30s"


def test_the_worker_check_is_guarded():
    worker = services("docker-compose.yml")["media-worker"]
    command = " ".join(worker["healthcheck"]["test"])
    assert GUARD in command and "app.backend.workers.alive" in command
    assert "media_worker" not in command, "an older image would start a second worker"


def test_the_signer_check_is_guarded():
    signer = services("docker-compose.pki.yml")["pki-signer"]
    command = " ".join(signer["healthcheck"]["test"])
    assert GUARD in command and "app.backend.signer.health" in command
    assert signer["network_mode"] == "none"


def test_e2e_api_waits_on_readiness():
    command = services("tests/docker/compose.test.yml")["api"]["healthcheck"]["test"][-1]
    assert "/health/ready" in command


def test_the_guarded_commands_pass_on_an_image_without_the_module(tmp_path):
    """An image from before the checks: find_spec answers None, the check passes"""
    for name, service in (("docker-compose.yml", "media-worker"), ("docker-compose.pki.yml", "pki-signer")):
        argv = services(name)[service]["healthcheck"]["test"]
        assert argv[:2] == ["CMD", "python"] and argv[2] == "-c"
        code = argv[3].replace("app.backend.workers.alive", "app.backend.no_such_module").replace(
            "app.backend.signer.health", "app.backend.no_such_module")
        # the image's working directory is /srv/app, where the package app is importable: the project root here
        result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, timeout=60)
        assert result.returncode == 0, (name, result.stderr)


# ---------------------------------------------------------------- signer check

@pytest.mark.skipif(os.name == "nt", reason="Unix sockets")
def test_signer_check_fails_without_a_socket(tmp_path, monkeypatch):
    from app.backend.signer import health
    monkeypatch.setenv("SIGNER_SOCKET_PATH", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("SIGNER_CA_CERT_PATH", "x")
    monkeypatch.setenv("SIGNER_CA_KEY_PATH", "x")
    assert health.main() == 1


def test_signer_check_passes_on_a_healthy_answer(monkeypatch):
    import httpx
    from app.backend.signer import health

    def handler(request):
        assert request.url.path == "/health"
        return httpx.Response(200, json={"subject": "CN=x", "not_after": "2030-01-01T00:00:00Z",
                                         "fingerprint_sha256": "ab"})

    monkeypatch.setattr(health, "_transport", lambda: httpx.MockTransport(handler))
    assert health.main() == 0


# ---------------------------------------------------------------- nginx

def test_nginx_proxies_readiness():
    text = (PROJECT_ROOT / "nginx-app.conf").read_text(encoding="utf-8")
    block = text.split("location = /health/ready {", 1)[1].split("}", 1)[0]
    assert "include /etc/nginx/snippets/watch-web-proxy.conf;" in block
    assert "proxy_pass http://watch_web_api;" in block
