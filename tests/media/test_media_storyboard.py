"""GET /media/{id}/storyboard (WebVTT of signed sheet URLs) and storyboard_status in the media responses."""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from app.backend.models.storyboard import VideoStoryboard
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media

VIEW = ["media.view"]


def storyboard_url(media_id: int) -> str:
    return V1.path(MediaRoutes.MEDIA_STORYBOARD, media_id=media_id)


async def _scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=VIEW, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, user, observation


async def _ready_video(db, observation, user, **fields):
    video = await make_media(
        db, observation, user, media_type="video", poster_status="ready", duration_seconds=125.0, **fields)
    db.add(VideoStoryboard(
        media_id=video.id, status="ready", attempts=1, step_seconds=5, frame_count=25,
        columns=10, rows=10, tile_width=160, tile_height=90, sheet_count=1,
    ))
    await db.commit()
    return video


@pytest.mark.asyncio
async def test_a_ready_storyboard_is_webvtt_with_signed_sheet_urls(client, db, headers):
    _, user, observation = await _scene(db)
    video = await _ready_video(db, observation, user)

    response = await client.get(storyboard_url(video.id), headers=headers(user))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/vtt")
    assert response.text.startswith("WEBVTT")
    urls = [line for line in response.text.splitlines() if "#xywh=" in line]
    assert len(urls) == 25
    # Absolute: the player resolves cue URLs against the track, a blob: URL, which cannot be a base.
    # Without public_base_url the origin is the request's (the test client's http://test)
    assert settings.storage.imgproxy.public_base_url == ""
    assert all(line.startswith("http://test" + settings.storage.imgproxy.public_prefix + "/") for line in urls)
    max_age = int(response.headers["cache-control"].split("max-age=")[1])
    assert 0 < max_age <= settings.storage.thumbnail.url_ttl_seconds
    assert "private" in response.headers["cache-control"]
    assert response.text.rstrip().splitlines()[-2].endswith("00:02:05.000")


@pytest.mark.asyncio
async def test_sheet_urls_use_the_configured_public_origin(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.storage.imgproxy, "public_base_url", "https://monitoring.example/")
    _, user, observation = await _scene(db)
    video = await _ready_video(db, observation, user)

    response = await client.get(storyboard_url(video.id), headers=headers(user))
    assert response.status_code == 200, response.text
    urls = [line for line in response.text.splitlines() if "#xywh=" in line]
    assert urls and all(
        line.startswith("https://monitoring.example" + settings.storage.imgproxy.public_prefix + "/") for line in urls
    )


@pytest.mark.asyncio
async def test_pending_answers_202_with_retry_after(client, db, headers):
    _, user, observation = await _scene(db)
    video = await make_media(db, observation, user, media_type="video", poster_status="ready", duration_seconds=125.0)
    db.add(VideoStoryboard(media_id=video.id, status="pending"))
    await db.commit()

    response = await client.get(storyboard_url(video.id), headers=headers(user))
    assert response.status_code == 202
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-storyboard-status"] == "pending"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b""


@pytest.mark.asyncio
async def test_failed_is_storyboard_failed(client, db, headers):
    _, user, observation = await _scene(db)
    video = await make_media(db, observation, user, media_type="video", poster_status="ready", duration_seconds=125.0)
    db.add(VideoStoryboard(media_id=video.id, status="failed", attempts=3))
    await db.commit()
    response = await client.get(storyboard_url(video.id), headers=headers(user))
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "storyboard_failed"
    assert body["attempts"] == 3

    # A failed poster has no storyboard row: no storyboard attempt was made
    no_poster = await make_media(db, observation, user, media_type="video", poster_status="failed", poster_attempts=3)
    response = await client.get(storyboard_url(no_poster.id), headers=headers(user))
    assert response.status_code == 409
    assert response.json()["attempts"] == 0


@pytest.mark.asyncio
async def test_sheet_urls_follow_the_generation_of_the_storyboard(client, db, headers):
    _, user, observation = await _scene(db)
    video = await _ready_video(db, observation, user)
    row = await db.get(VideoStoryboard, video.id)

    async def sheet_urls(generated_at: datetime) -> list:
        row.generated_at = generated_at
        db.add(row)
        await db.commit()
        response = await client.get(storyboard_url(video.id), headers=headers(user))
        assert response.status_code == 200, response.text
        return [line for line in response.text.splitlines() if "#xywh=" in line]

    first = datetime(2026, 10, 6, 12, 0, 0)
    urls = await sheet_urls(first)
    assert all(f"/cb:{int(first.replace(tzinfo=timezone.utc).timestamp())}/" in url for url in urls)
    assert await sheet_urls(first) == urls, "the same generation keeps the cache key"
    assert set(await sheet_urls(first + timedelta(minutes=5))).isdisjoint(urls), "a regeneration gets new URLs"


@pytest.mark.parametrize("broken", ["tile_height", "frame_count", "duration"])
@pytest.mark.asyncio
async def test_a_ready_row_without_a_usable_layout_is_storyboard_failed(client, db, headers, broken):
    # Never a 500 from build_vtt: the row says ready, but what the WebVTT needs is not there
    _, user, observation = await _scene(db)
    duration = None if broken == "duration" else 125.0
    video = await make_media(db, observation, user, media_type="video", poster_status="ready", duration_seconds=duration)
    layout = dict(step_seconds=5, frame_count=25, columns=10, rows=10, tile_width=160, tile_height=90, sheet_count=1)
    if broken != "duration":
        layout[broken] = None
    db.add(VideoStoryboard(media_id=video.id, status="ready", attempts=2, **layout))
    await db.commit()

    response = await client.get(storyboard_url(video.id), headers=headers(user))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "storyboard_failed"
    assert response.json()["attempts"] == 2
    assert response.headers["x-storyboard-status"] == "failed"


@pytest.mark.asyncio
async def test_the_answer_follows_the_row_the_route_loaded(client, db, headers, monkeypatch):
    # The job status read with the media row (column_property) may be older than the row the route loads next,
    # e.g. reconciliation requeued it because its sheets are gone: the loaded row decides
    from sqlalchemy import update
    from app.backend.api.v1 import media as media_routes

    _, user, observation = await _scene(db)
    video = await _ready_video(db, observation, user)
    real = media_routes.get_observation_from_media

    async def requeued_meanwhile(session, media_file):
        await session.execute(
            update(VideoStoryboard).where(VideoStoryboard.media_id == media_file.id).values(status="pending")
        )
        return await real(session, media_file)

    monkeypatch.setattr(media_routes, "get_observation_from_media", requeued_meanwhile)
    response = await client.get(storyboard_url(video.id), headers=headers(user))
    assert response.status_code == 202, response.text
    assert response.headers["x-storyboard-status"] == "pending"


@pytest.mark.asyncio
async def test_unsupported_cases(client, db, headers, monkeypatch):
    _, user, observation = await _scene(db)
    image = await make_media(db, observation, user)
    short = await make_media(db, observation, user, media_type="video", poster_status="ready", duration_seconds=30.0)
    off = await _ready_video(db, observation, user)

    for media in (image, short):
        response = await client.get(storyboard_url(media.id), headers=headers(user))
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "storyboard_unsupported"
        assert response.json()["min_duration_seconds"] == 60

    monkeypatch.setattr(settings.storage.storyboard, "enabled", False)
    response = await client.get(storyboard_url(off.id), headers=headers(user))
    assert response.status_code == 422
    assert response.json()["code"] == "storyboard_unsupported"
    assert response.json()["min_duration_seconds"] == 60


@pytest.mark.asyncio
async def test_post_scope_and_archived(client, db, headers):
    _, user, observation = await _scene(db)
    video = await _ready_video(db, observation, user)
    other_post = await make_post(db)
    outsider = await make_user(db, perms=VIEW, post_id=other_post.id)
    denied = await client.get(storyboard_url(video.id), headers=headers(outsider))
    assert denied.status_code == 403

    archived = await _ready_video(db, observation, user, is_deleted=True)
    gone = await client.get(storyboard_url(archived.id), headers=headers(user))
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_media_responses_carry_storyboard_status(client, db, headers):
    _, user, observation = await _scene(db)
    video = await _ready_video(db, observation, user)
    image = await make_media(db, observation, user)

    info = await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=video.id), headers=headers(user))
    assert info.status_code == 200, info.text
    assert info.json()["storyboard_status"] == "ready"
    info = await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=image.id), headers=headers(user))
    assert info.json()["storyboard_status"] == "unsupported"
