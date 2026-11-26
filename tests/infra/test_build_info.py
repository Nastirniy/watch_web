"""Build info: the release tag and the commit of the image, in /health and the worker heartbeat (RELEASE.md)"""
import json
import re

import pytest

from app.backend import build_info as build_info_module
from app.backend.build_info import BuildInfo, build_info
from app.backend.config import Settings, settings
from app.backend.enums.routes import SystemRoutes
from tests.paths import PROJECT_ROOT


@pytest.fixture
def info_file(tmp_path, monkeypatch):
    path = tmp_path / "build_info.json"
    monkeypatch.setattr(build_info_module, "BUILD_INFO_PATH", path)
    build_info.cache_clear()
    yield path
    build_info.cache_clear()


def test_reads_the_file_the_image_writes(info_file):
    info_file.write_text(json.dumps({
        "version": "2026.11.24-2a8c1e7", "revision": "2a8c1e7" + "0" * 33, "created": "2026-11-24T09:12:00Z",
    }))
    assert build_info() == BuildInfo("2026.11.24-2a8c1e7", "2a8c1e7" + "0" * 33, "2026-11-24T09:12:00Z")


def test_dev_without_the_file(info_file):
    assert build_info() == BuildInfo("dev", None, None)


@pytest.mark.parametrize("content", ["not json", "[]", '{"revision": "abc"}', '{"version": ""}', '{"version": 7}'])
def test_a_malformed_file_stops_the_start(info_file, content):
    info_file.write_text(content)
    with pytest.raises(ValueError, match="build_info.json"):
        build_info()


async def test_health_reports_version_and_revision(client, db, monkeypatch, info_file):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)
    info_file.write_text(json.dumps({"version": "2026.11.24-2a8c1e7", "revision": "f" * 40, "created": None}))
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["version"] == "2026.11.24-2a8c1e7"
    assert body["revision"] == "f" * 40


async def test_health_revision_is_null_in_development(client, db, monkeypatch, info_file):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["version"] == "dev"
    assert body["revision"] is None


def test_api_document_version_does_not_follow_the_build(info_file):
    from app.main import API_DOCUMENT_VERSION, fastapi_app
    info_file.write_text(json.dumps({"version": "2026.11.24-2a8c1e7", "revision": None, "created": None}))
    assert API_DOCUMENT_VERSION == "1.0.0"
    assert fastapi_app.version == "1.0.0"


def test_a_leftover_app_version_variable_does_not_break_the_settings(monkeypatch):
    monkeypatch.setenv("MYSETTINGS_APP__VERSION", "9.9.9")
    loaded = Settings()
    assert not hasattr(loaded.app, "version")


def test_worker_heartbeat_details_name_the_version(info_file, tmp_path):
    from app.backend.workers.media_worker import MediaWorker
    info_file.write_text(json.dumps({"version": "2026.11.24-2a8c1e7", "revision": None, "created": None}))
    worker = MediaWorker(lambda: None, tmp_path, name="media@test:1", concurrency=4, extractor=lambda s, t: None)
    assert worker.heartbeat_details().endswith(" version=2026.11.24-2a8c1e7")


DOCKERFILE = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["WATCH_WEB_VERSION", "WATCH_WEB_REVISION", "WATCH_WEB_CREATED"])
def test_dockerfile_declares_the_build_args(name):
    assert re.search(rf"^ARG {name}(=|\s*$)", DOCKERFILE, re.M), name


@pytest.mark.parametrize("label, arg", [
    ("org.opencontainers.image.version", "WATCH_WEB_VERSION"),
    ("org.opencontainers.image.revision", "WATCH_WEB_REVISION"),
    ("org.opencontainers.image.created", "WATCH_WEB_CREATED"),
])
def test_dockerfile_labels_the_image(label, arg):
    assert re.search(rf'{re.escape(label)}="\$\{{?{arg}\}}?"', DOCKERFILE), label


def test_dockerfile_writes_build_info_and_package_list():
    assert "/srv/app/app/build_info.json" in DOCKERFILE
    assert "dpkg-query -W" in DOCKERFILE and "/srv/app/app/packages.txt" in DOCKERFILE


def test_build_info_path_is_the_one_the_dockerfile_writes():
    # /srv/app is the WORKDIR: app/backend/build_info.py -> parents[1] is /srv/app/app
    assert build_info_module.BUILD_INFO_PATH.parent.name == "app"
    assert build_info_module.BUILD_INFO_PATH.name == "build_info.json"
