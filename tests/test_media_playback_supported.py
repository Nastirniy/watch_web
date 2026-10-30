"""playback_supported: the advice of a media answer whether the web shows a player (owner decision 2026-10-30).

Derived, never stored: false for Matroska, true for every other video and audio, null for media that is not played.
It is advice to the UI, not a refusal: the stream route keeps serving a Matroska file.
"""
import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.general import MediaType
from app.backend.enums.media_types import ContentType
from app.backend.enums.routes import MediaRoutes, SearchRoutes
from app.backend.models.media import MediaFile
from tests.helpers import make_dictionary, make_media, make_observation, make_post, make_user

MKV_PLAYBACK = 'video/matroska; codecs="avc1.640015, mp4a.40.2"'


async def _setup(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view", "observation.view"], post_id=post.id)
    return user, await make_observation(db, user, post, ids["signal_type_id"])


async def _matroska(db, observation, user, **fields):
    return await make_media(db, observation, user, media_type="video", file_extension=".mkv",
                            mime_type=ContentType.VIDEO_MATROSKA.value, **fields)


async def _info(client, media_id, headers):
    response = await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=media_id), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_matroska_is_not_playable_and_keeps_its_playback_type(client, db, headers):
    user, observation = await _setup(db)
    media = await _matroska(db, observation, user, playback_type=MKV_PLAYBACK)
    body = await _info(client, media.id, headers(user))
    assert body["playback_supported"] is False
    assert body["playback_type"] == MKV_PLAYBACK and body["mime_type"] == "video/matroska"


async def test_a_matroska_file_not_probed_yet_is_not_playable_either(client, db, headers):
    user, observation = await _setup(db)
    media = await _matroska(db, observation, user)
    body = await _info(client, media.id, headers(user))
    assert body["playback_supported"] is False and body["playback_type"] == "video/matroska"


@pytest.mark.parametrize("media_type,mime_type", [
    ("video", ContentType.VIDEO_MP4), ("video", ContentType.VIDEO_WEBM), ("video", ContentType.VIDEO_QUICKTIME),
    ("audio", ContentType.AUDIO_MPEG), ("audio", ContentType.AUDIO_OGG),
])
async def test_every_other_video_and_audio_is_playable(client, db, headers, media_type, mime_type):
    user, observation = await _setup(db)
    media = await make_media(db, observation, user, media_type=media_type, mime_type=mime_type.value)
    assert (await _info(client, media.id, headers(user)))["playback_supported"] is True


@pytest.mark.parametrize("media_type", [MediaType.IMAGE, MediaType.DOCUMENT])
async def test_media_that_is_not_played_has_no_answer(client, db, headers, media_type):
    user, observation = await _setup(db)
    media = await make_media(db, observation, user)
    media.media_type = media_type.value
    db.add(media)
    await db.commit()
    assert (await _info(client, media.id, headers(user)))["playback_supported"] is None


async def test_every_media_answer_carries_it(client, db, headers):
    user, observation = await _setup(db)
    media = await _matroska(db, observation, user)
    h = headers(user)
    listed = await client.get(V1.path(MediaRoutes.ROOT), headers=h)
    assert listed.status_code == 200, listed.text
    assert [item["playback_supported"] for item in listed.json()] == [False]
    searched = await client.get(V1.path(SearchRoutes.MEDIA), headers=h)
    assert searched.status_code == 200, searched.text
    assert [item["playback_supported"] for item in searched.json()["items"]] == [False]
    of_observation = await client.post(V1.path(MediaRoutes.LIST_ALL_ATTACHED_MEDIA), headers=h,
                                       json={"observation_id": observation.id})
    assert of_observation.status_code == 200, of_observation.text
    assert [(item["id"], item["playback_supported"]) for item in of_observation.json()] == [(media.id, False)]


def test_it_is_derived_never_stored():
    assert "playback_supported" not in MediaFile.model_fields


@pytest.mark.parametrize("enable_nginx", [True, False])
async def test_the_stream_route_keeps_serving_a_matroska_file(client, db, headers, tmp_path, monkeypatch, enable_nginx):
    """The flag is advice to the UI: an API client may still fetch the bytes"""
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    user, observation = await _setup(db)
    media = await _matroska(db, observation, user)
    (tmp_path / media.file_path).parent.mkdir(parents=True)
    (tmp_path / media.file_path).write_bytes(b"matroska bytes")

    response = await client.get(V1.path(MediaRoutes.MEDIA_STREAM, media_id=media.id), headers=headers(user))
    assert response.status_code == 200, response.text
    if enable_nginx:
        assert response.headers["x-accel-redirect"].endswith(media.file_path)
    else:
        assert response.content == b"matroska bytes"
        assert response.headers["content-type"].startswith(ContentType.VIDEO_MATROSKA.value)


@pytest.mark.asyncio
async def test_a_webm_file_that_kept_the_old_sniffed_name_stays_playable(client, db, headers):
    """WebM is a subset of Matroska: an old row of a .webm file may hold video/x-matroska (migration 032 leaves it)"""
    user, observation = await _setup(db)
    media = await make_media(db, observation, user, media_type="video", file_extension=".webm",
                             mime_type=ContentType.VIDEO_X_MATROSKA.value)
    assert (await _info(client, media.id, headers(user)))["playback_supported"] is True
