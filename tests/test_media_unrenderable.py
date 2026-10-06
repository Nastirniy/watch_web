"""Media whose thumbnails can never be rendered: images above imgproxy's resolution limit and video files without a
video stream. Both are unsupported from the start, with a problem that says why, never a link that always fails."""
import io
import subprocess

import pytest

from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from app.backend.api.v1 import V1
from app.backend.models.media import MediaFile
from app.backend.services.media import jobs, renderer, urls
from app.backend.services.media.manager import reset_media_manager
from app.backend.services.media.posters import resolve_binary
from app.backend.workers.media_worker import MediaWorker
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media

FFMPEG = resolve_binary("ffmpeg", imageio_fallback=True)
UPLOAD = V1.path(MediaRoutes.UPLOAD)


def thumb_url(media_id: int) -> str:
    return V1.path(MediaRoutes.MEDIA_THUMBNAIL, media_id=media_id)


def share_url(media_id: int) -> str:
    return V1.path(MediaRoutes.MEDIA_SHARE, media_id=media_id)


def info_url(media_id: int) -> str:
    return V1.path(MediaRoutes.MEDIA_INFO, media_id=media_id)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "_tmp"))
    reset_media_manager()
    yield tmp_path
    reset_media_manager()


async def _scene(db, perms=("media.view", "media.create")):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=list(perms), post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return user, observation


async def _reload(db, media_id: int) -> MediaFile:
    return await db.get(MediaFile, media_id, populate_existing=True)


# ----------------------------------------------------------------- images above the render limit

@pytest.mark.asyncio
async def test_an_image_above_the_render_limit_is_unsupported(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    monkeypatch.setattr(settings.storage.imgproxy, "max_src_resolution_mp", 60)
    monkeypatch.setattr(settings.storage.imgproxy, "prewarm_base_url", "http://nginx")
    user, observation = await _scene(db)
    huge = await make_media(db, observation, user, width=8000, height=8000)
    at_limit = await make_media(db, observation, user, width=7500, height=8000)
    unknown = await make_media(db, observation, user)

    info = (await client.get(info_url(huge.id), headers=headers(user))).json()
    assert info["thumbnail_status"] == "unsupported" and info["thumbnail_urls"] is None

    response = await client.get(thumb_url(huge.id), headers=headers(user))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "image_too_large_for_thumbnails"
    assert body["megapixels"] == 64 and body["max_megapixels"] == 60
    assert "x-accel-redirect" not in response.headers

    shared = await client.post(share_url(huge.id), json={"size": "large", "ttl_seconds": 600}, headers=headers(user))
    assert shared.status_code == 422 and shared.json()["code"] == "image_too_large_for_thumbnails"
    assert urls.prewarm_url(huge, "medium", "auto") is None, "no pre-warm of a render imgproxy refuses"

    for media in (at_limit, unknown):
        ok = await client.get(thumb_url(media.id), headers=headers(user))
        assert ok.status_code == 200 and ok.headers["x-accel-redirect"], "at the limit, or size unknown: rendered as before"


@pytest.mark.asyncio
async def test_a_200_mp_phone_photo_is_rendered_with_the_default_limit(client, db, headers):
    """The limit guards against small files holding gigapixels, not against large photos"""
    user, observation = await _scene(db)
    photo = await make_media(db, observation, user, width=16320, height=12240)
    info = (await client.get(info_url(photo.id), headers=headers(user))).json()
    assert info["thumbnail_status"] == "ready" and set(info["thumbnail_urls"]) == {"small", "medium", "large", "preview"}


@pytest.mark.asyncio
async def test_an_uploaded_image_above_the_limit_is_stored_without_thumbnails(client, db, headers, storage, monkeypatch):
    """The original is kept (it is the evidence); only the thumbnails are unavailable"""
    from PIL import Image
    monkeypatch.setattr(settings.storage.imgproxy, "max_src_resolution_mp", 0.01)
    warmed = []
    monkeypatch.setattr(renderer, "prewarm", lambda url: warmed.append(url))
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    monkeypatch.setattr(settings.storage.imgproxy, "prewarm_base_url", "http://nginx")
    user, observation = await _scene(db)
    png = io.BytesIO()
    Image.new("RGB", (200, 100), (10, 120, 200)).save(png, format="PNG")

    response = await client.post(UPLOAD, data={"observation_id": observation.id, "media_type": "image"},
                                 files={"file": ("big.png", png.getvalue(), "image/png")}, headers=headers(user))
    assert response.status_code == 200, response.text
    assert response.json()["thumbnail_status"] == "unsupported" and response.json()["thumbnail_urls"] is None
    row = await _reload(db, response.json()["media_file_id"])
    assert (row.width, row.height) == (200, 100)
    assert warmed == []


# ----------------------------------------------------------------- video files without a video stream

@pytest.mark.asyncio
async def test_a_video_file_without_a_video_stream_is_unsupported(client, db, headers):
    user, observation = await _scene(db)
    audio_only = await make_media(db, observation, user, media_type="video", poster_status="unsupported",
                                  audio_codec="aac", duration_seconds=120.0)

    info = (await client.get(info_url(audio_only.id), headers=headers(user))).json()
    assert info["thumbnail_status"] == "unsupported" and info["thumbnail_urls"] is None
    assert info["storyboard_status"] == "unsupported"

    for response in (
        await client.get(thumb_url(audio_only.id), headers=headers(user)),
        await client.post(share_url(audio_only.id), json={"size": "large", "ttl_seconds": 600}, headers=headers(user)),
        await client.get(V1.path(MediaRoutes.MEDIA_STORYBOARD, media_id=audio_only.id), headers=headers(user)),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "no_video_stream"


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")
@pytest.mark.asyncio
async def test_an_uploaded_video_file_without_a_video_stream_is_never_queued(client, db, headers, storage, tmp_path):
    user, observation = await _scene(db)
    source = tmp_path / "recording.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", "-f", "mp4", str(source)], check=True, timeout=120)

    response = await client.post(UPLOAD, data={"observation_id": observation.id, "media_type": "video"},
                                 files={"file": ("recording.mp4", source.read_bytes(), "video/mp4")}, headers=headers(user))
    assert response.status_code == 200, response.text
    assert response.json()["thumbnail_status"] == "unsupported"
    row = await _reload(db, response.json()["media_file_id"])
    assert row.poster_status == "unsupported" and row.audio_codec == "aac" and row.video_codec is None
    assert await jobs.claim_next_poster(db, "media@test:1") is None, "nothing for the worker to try"


@pytest.mark.asyncio
async def test_unsupported_replaces_only_a_pending_poster_job(db):
    user, observation = await _scene(db)
    pending = await make_media(db, observation, user, media_type="video")
    taken = await make_media(db, observation, user, media_type="video")
    assert (await jobs.claim_next_poster(db, "media@test:1")).id == pending.id
    other = await make_media(db, observation, user, media_type="video")

    assert await jobs.mark_poster_unsupported(db, other.id) is True
    assert await jobs.mark_poster_unsupported(db, pending.id) is False, "a claimed job belongs to its worker"
    assert (await _reload(db, other.id)).poster_status == "unsupported"
    assert (await _reload(db, pending.id)).poster_status == "processing"
    assert (await jobs.claim_next_poster(db, "media@test:1")).id == taken.id
    assert await jobs.claim_next_poster(db, "media@test:1") is None, "an unsupported row is never claimed"
    counts = await jobs.poster_counts(db)
    assert counts["unsupported"] == 1 and counts["processing"] == 2


@pytest.mark.asyncio
async def test_the_worker_marks_a_video_without_a_video_stream_unsupported(db, session_maker, tmp_path, monkeypatch):
    """Probed at upload (an administrator queued it again) or probed by the worker: no extraction, no retries"""
    user, observation = await _scene(db)
    known = await make_media(db, observation, user, media_type="video", audio_codec="aac", duration_seconds=30.0,
                             playback_type='video/mp4; codecs="mp4a.40.2"')
    unprobed = await make_media(db, observation, user, media_type="video")
    probed = {"duration_seconds": 30.0, "video_codec": None, "audio_codec": "opus", "playback_type": 'video/webm; codecs="opus"'}
    monkeypatch.setattr(MediaWorker, "_probe", lambda self, source, mime_type: dict(probed))
    extracted = []

    worker = MediaWorker(session_maker, tmp_path, name="media@test:1", extractor=lambda source, target: extracted.append(source))
    assert await worker.claim_and_run_once() is True
    assert await worker.claim_and_run_once() is True
    assert await worker.claim_and_run_once() is False

    assert extracted == []
    for media_id in (known.id, unprobed.id):
        row = await _reload(db, media_id)
        assert row.poster_status == "unsupported" and row.poster_locked_until is None and row.poster_worker == "media@test:1"
    row = await _reload(db, unprobed.id)
    assert row.audio_codec == "opus" and row.duration_seconds == 30.0, "the worker keeps what its probe found"
    assert worker.jobs_failed == 0
