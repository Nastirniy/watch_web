"""Compose and Dockerfile: one release tag for every watch-web service, pinned third-party images (RELEASE.md)"""
import json
import re

import pytest
import yaml

from tests.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
PRODUCTION_FILES = ("docker-compose.yml", "docker-compose.pki.yml", "docker-compose.backup.yml",
                    "docker-compose.backup-copy.yml")
WATCH_WEB_IMAGE = "watch-web:${WATCH_WEB_TAG:?set WATCH_WEB_TAG in .env (see RELEASE.md)}"
IMGPROXY_IMAGE = "${IMGPROXY_IMAGE:?set IMGPROXY_IMAGE in .env (see RELEASE.md)}"


def services(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"]


def watch_web_services():
    for name in PRODUCTION_FILES:
        for service, spec in services(name).items():
            image = spec.get("image", "")
            # api and media-worker take the image from the x-app anchor of docker-compose.yml
            if "watch-web" in image or (name == "docker-compose.yml" and service in ("api", "media-worker")):
                yield name, service, spec


def test_every_watch_web_service_runs_the_one_release_tag():
    found = {(name, service) for name, service, _ in watch_web_services()}
    assert found == {
        ("docker-compose.yml", "api"), ("docker-compose.yml", "media-worker"), ("docker-compose.yml", "migrate"),
        ("docker-compose.pki.yml", "pki-signer"), ("docker-compose.backup.yml", "db-backup"),
    }
    for name, service, spec in watch_web_services():
        assert spec["image"] == WATCH_WEB_IMAGE, (name, service)
        assert spec["pull_policy"] == "never", (name, service)


@pytest.mark.parametrize("name", PRODUCTION_FILES)
def test_no_production_compose_file_builds(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not re.search(r"^\s*build\s*:", text, re.M), name
    assert "--build" not in text, name


def test_imgproxy_image_comes_from_env():
    assert services("docker-compose.yml")["imgproxy"]["image"] == IMGPROXY_IMAGE


def test_env_example_names_the_release_variables():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("WATCH_WEB_TAG=", "IMGPROXY_IMAGE=", "IMGPROXY_IMAGE_ID="):
        assert re.search(rf"^#? ?{name}", text, re.M), name
    assert "RELEASE.md" in text


DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_python_base_image_is_pinned_by_digest():
    match = re.search(r"^ARG PYTHON_BASE_IMAGE=(\S+)$", DOCKERFILE, re.M)
    assert match, "ARG PYTHON_BASE_IMAGE missing"
    assert re.fullmatch(r"python:3\.13-slim@sha256:[0-9a-f]{64}", match.group(1)), match.group(1)
    assert re.search(r"^FROM \$\{PYTHON_BASE_IMAGE\}$", DOCKERFILE, re.M)
    assert DOCKERFILE.index("ARG PYTHON_BASE_IMAGE") < DOCKERFILE.index("FROM ${PYTHON_BASE_IMAGE}")


def test_migrate_tool_service():
    migrate = services("docker-compose.yml")["migrate"]
    assert migrate["profiles"] == ["tools"]
    assert migrate["entrypoint"] == ["python", "-m", "app.backend.cli.migrations_manager", "migrate"]
    assert migrate["env_file"] == ".env"
    assert migrate["network_mode"] == "host"
    assert "restart" not in migrate and "ports" not in migrate
    assert "healthcheck" not in migrate and "depends_on" not in migrate


def test_graceful_stop_values():
    """Q1.3 a: uvicorn is PID 1 (exec) and ends open requests within 25 s, below the api's 30 s grace; the worker
    gives back its claims within 90 s"""
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M).group(1))
    assert cmd[:2] == ["sh", "-c"] and "exec python -m uvicorn " in cmd[2]
    graceful = int(re.search(r"--timeout-graceful-shutdown (\d+)", cmd[2]).group(1))
    main = services("docker-compose.yml")
    assert graceful == 25
    assert graceful < int(main["api"]["stop_grace_period"].rstrip("s")) == 30
    assert main["media-worker"]["stop_grace_period"] == "90s"


def test_e2e_stack_uses_a_tag_and_the_imgproxy_variable():
    e2e = services("tests/docker/compose.test.yml")
    text = (ROOT / "tests" / "docker" / "compose.test.yml").read_text(encoding="utf-8")
    for service in ("api", "media-worker"):
        assert e2e[service]["image"] == "watch-web:${WATCH_WEB_TAG:-e2e}", service
        assert e2e[service]["pull_policy"] == "never", service
    assert e2e["imgproxy"]["image"].startswith("${IMGPROXY_IMAGE:-ghcr.io/imgproxy/imgproxy:v3")
    assert "docker build -t watch-web:e2e ." in text


@pytest.mark.parametrize("name", PRODUCTION_FILES)
def test_every_service_rotates_its_docker_log(name):
    """Container logs are files on the host: every service caps them (json-file, max-size, max-file), so a busy
    api fills at most max-size x max-file per container"""
    for service, spec in services(name).items():
        if "logging" not in spec and name != "docker-compose.yml":
            # an override service that only adds to a base service takes the base's logging
            continue
        logging = spec.get("logging")
        assert logging and logging["driver"] == "json-file", (name, service)
        assert logging["options"]["max-size"] and int(logging["options"]["max-file"]) >= 1, (name, service)


def test_containers_log_to_stdout_only():
    """In a container the app logs to stdout, which Docker rotates: a log file would land in the container's
    writable layer (gone at the next deploy, outside the rotation, written by 4 uvicorn processes at once) and its
    folder is not writable for MEDIA_UID, so the start would fail"""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^MYSETTINGS_LOGGING__ENABLE_FILE_LOGGING=false$", text, re.M)
