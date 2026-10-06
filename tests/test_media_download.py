"""Originals (download, stream): behind nginx the API hands the file over (X-Accel-Redirect), without nginx it
serves the file itself. Either way the API decides whether the file is there: a missing file is the API's
media_file_missing problem, never nginx's own 404 page (an error the API makes is a problem; what nginx makes
on its own belongs to the infrastructure, API.md)."""
import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from tests.helpers import make_dictionary, make_media, make_observation, make_post, make_user

ORIGINAL_ROUTES = (MediaRoutes.MEDIA_DOWNLOAD, MediaRoutes.MEDIA_STREAM)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    return tmp_path


async def _media(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return user, await make_media(db, observation, user)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
async def test_an_original_on_disk_is_handed_to_nginx(client, db, headers, storage, monkeypatch, route):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    user, media = await _media(db)
    (storage / media.file_path).parent.mkdir(parents=True)
    (storage / media.file_path).write_bytes(b"original")
    response = await client.get(V1.path(route, media_id=media.id), headers=headers(user))
    assert response.status_code == 200
    assert response.headers["x-accel-redirect"].endswith(media.file_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [True, False])
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
async def test_an_original_missing_on_disk_is_media_file_missing(client, db, headers, storage, monkeypatch, route,
                                                                 enable_nginx):
    """Behind nginx the API handed the path over unchecked, and nginx answered its own HTML 404"""
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    user, media = await _media(db)
    response = await client.get(V1.path(route, media_id=media.id), headers=headers(user))
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "media_file_missing"
