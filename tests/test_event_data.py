"""Events of the work screens: observation.changed and media.changed. One event per operation, in the transaction of
the change after its audit row; ids and enum values only; at the post of the observation, to the holders of
observation.view / media.view in effect there; a refused write publishes nothing; the media worker publishes
media.changed processed when a poster or a storyboard reaches a state a page shows (ready, failed, unsupported)."""
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.context import load_auth_context
from app.backend.auth.scope import Node
from app.backend.config import EventsConfig, settings
from app.backend.enums.audit import AuditCategory, AuditEvent
from app.backend.enums.events import (AUDIT_EVENT_TYPES, MEDIA_CHANGES, OBSERVATION_CHANGES, EventType, MediaChange,
                                      ObservationChange)
from app.backend.enums.general import Permissions as P, UserType
from app.backend.enums.routes import MediaRoutes, ObservationRoutes, PostRoutes
from app.backend.models.outbox_event import OutboxEvent
from app.backend.models.storyboard import VideoStoryboard
from app.backend.services.media import jobs, storyboard_jobs
from app.backend.services.media.manager import reset_media_manager
from app.backend.services.media.posters import PosterError
from app.backend.services.media.storyboards import StoryboardPlan, StoryboardResult
from app.backend.services.events.hub import EventHub
from app.backend.workers.media_worker import MediaWorker
from tests.helpers import (give, make_admin, make_dictionary, make_group, make_media, make_observation, make_post,
                           make_user, observation_payload)
from tests.test_event_hub import TextRenderer, later, queued

DATA_TYPES = {EventType.OBSERVATION_CHANGED, EventType.MEDIA_CHANGED}


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "_tmp"))
    reset_media_manager()
    yield tmp_path
    reset_media_manager()


async def _rows(db, *types: EventType) -> list:
    query = select(OutboxEvent).order_by(OutboxEvent.id).execution_options(populate_existing=True)
    query = query.where(OutboxEvent.type.in_([t.value for t in (types or DATA_TYPES)]))
    return list((await db.exec(query)).all())


def _summary(rows) -> list:
    return [(row.type, row.scope_key, row.permission, row.user_id, row.conflate_key, json.loads(row.payload))
            for row in rows]


def _observation(observation_id: int, post_id: int, change: str) -> tuple:
    return ("observation.changed", f"p:{post_id}", "observation.view", None, f"observation:{observation_id}",
            {"observation_id": observation_id, "observation_change": change})


def _media(media_id: int, observation_id: int, post_id: int, change: str) -> tuple:
    return ("media.changed", f"p:{post_id}", "media.view", None, f"media:{media_id}",
            {"media_id": media_id, "observation_id": observation_id, "media_change": change})


def _png() -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24)).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- the catalog and its map

def test_every_audited_event_of_the_data_category_is_mapped():
    data = {event for event in AuditEvent if event.category is AuditCategory.DATA}
    assert data <= set(AUDIT_EVENT_TYPES)
    assert {AUDIT_EVENT_TYPES[event] for event in data} == DATA_TYPES | {None}
    for event in (AuditEvent.MEDIA_SHARE, AuditEvent.UPLOAD_SESSION_CREATE, AuditEvent.UPLOAD_SESSION_ABORT):
        assert AUDIT_EVENT_TYPES[event] is None, "no change of what a page shows"


@pytest.mark.parametrize("event", sorted(DATA_TYPES, key=lambda e: e.value))
def test_params_are_ids_and_enum_values_only(event):
    for name, annotation in event.params.items():
        assert name != "user_id", "publish() targets a user by that name"
        assert annotation is int or name in ("observation_change", "media_change"), "never optional"


def test_the_changes_are_the_enums():
    assert set(get_args(OBSERVATION_CHANGES)) == {c.value for c in ObservationChange} == {
        "created", "updated", "archived", "restored"}
    assert set(get_args(MEDIA_CHANGES)) == {c.value for c in MediaChange} == {
        "created", "archived", "restored", "processed"}


# ---------------------------------------------------------------- observation.changed

async def _observation_scene(db, perms=(P.OBSERVATION_VIEW, P.OBSERVATION_CREATE, P.OBSERVATION_UPDATE,
                                        P.OBSERVATION_ARCHIVE)):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, list(perms), post_id=post.id)
    return post, ids, user


async def test_observation_writes_publish_one_observation_changed_each(client, db, headers, events_on):
    post, ids, user = await _observation_scene(db)
    h = headers(user)
    created = await client.post(V1.path(ObservationRoutes.CREATE), headers=h,
                                json={**observation_payload(post.id, ids["signal_type_id"]), "tag_ids": [ids["tag_id"]]})
    assert created.status_code == 200, created.text
    obs_id = created.json()["id"]
    by_id = V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=obs_id)
    assert (await client.put(by_id, headers=h, json={"comment": "seen"})).status_code == 200
    assert (await client.put(by_id, headers=h, json={"tag_ids": []})).status_code == 200
    disappeared = await client.post(V1.path(ObservationRoutes.MARK_DISAPPEARED, observation_id=obs_id), headers=h,
                                    json={"disappeared_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    assert disappeared.status_code == 200, disappeared.text
    for route in (ObservationRoutes.OBSERVATION_ARCHIVE, ObservationRoutes.OBSERVATION_RESTORE):
        done = await client.post(V1.path(route, observation_id=obs_id), headers=h)
        assert done.status_code == 200, done.text

    assert _summary(await _rows(db)) == [
        _observation(obs_id, post.id, "created"), _observation(obs_id, post.id, "updated"),
        _observation(obs_id, post.id, "updated"), _observation(obs_id, post.id, "updated"),
        _observation(obs_id, post.id, "archived"), _observation(obs_id, post.id, "restored")]


async def test_band_changes_publish_observation_changed_updated(client, db, headers, events_on):
    post, ids, user = await _observation_scene(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    h = headers(user)
    bands = V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES, observation_id=observation.id)
    added = await client.post(bands, headers=h, json={"kind": "rc", "frequency_min": "300", "frequency_max": "400"})
    assert added.status_code == 200, added.text
    band = dict(observation_id=observation.id, frequency_id=added.json()["id"])
    assert (await client.put(V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID, **band), headers=h,
                             json={"frequency_max": "450"})).status_code == 200
    for route in (ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE, ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE):
        assert (await client.post(V1.path(route, **band), headers=h)).status_code == 200
    assert _summary(await _rows(db)) == [_observation(observation.id, post.id, "updated")] * 4


async def test_refused_observation_writes_publish_nothing(client, db, headers, events_on):
    post, ids, user = await _observation_scene(db)
    reader = await make_user(db, [P.OBSERVATION_VIEW], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    by_id = V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=observation.id)
    assert (await client.put(by_id, headers=headers(reader), json={"comment": "x"})).status_code == 403
    assert (await client.post(V1.path(ObservationRoutes.CREATE), headers=headers(reader),
                              json=observation_payload(post.id, ids["signal_type_id"]))).status_code == 403
    restore = V1.path(ObservationRoutes.OBSERVATION_RESTORE, observation_id=observation.id)
    assert (await client.post(restore, headers=headers(user))).status_code == 409, "not archived"
    bands =(await client.get(V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES, observation_id=observation.id),
                              headers=headers(user))).json()
    last = V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE, observation_id=observation.id,
                   frequency_id=bands[0]["id"])
    assert (await client.post(last, headers=headers(user))).status_code == 409, "the last band stays"
    assert await _rows(db) == []


async def test_a_post_archive_publishes_no_event_per_observation(client, db, headers, events_on):
    post, ids, user = await _observation_scene(db)
    for _ in range(3):
        await make_observation(db, user, post, ids["signal_type_id"])
    admin = await make_admin(db)
    archived = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert await _rows(db) == []
    assert [row.type for row in await _rows(db, EventType.SCOPE_CHANGED)] == ["scope.changed"]


async def test_events_off_publish_nothing_but_the_writes_still_work(client, db, headers):
    post, ids, user = await _observation_scene(db)
    created = await client.post(V1.path(ObservationRoutes.CREATE), headers=headers(user),
                                json=observation_payload(post.id, ids["signal_type_id"]))
    assert created.status_code == 200, created.text
    assert await _rows(db) == []


# ---------------------------------------------------------------- media.changed from the routes

async def _media_scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, [P.MEDIA_VIEW, P.MEDIA_CREATE, P.MEDIA_ARCHIVE, P.OBSERVATION_VIEW], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, user, observation


async def test_media_writes_publish_one_media_changed_each(client, db, headers, storage, events_on):
    post, user, observation = await _media_scene(db)
    h = headers(user)
    uploaded = await client.post(V1.path(MediaRoutes.UPLOAD), headers=h,
                                 data={"observation_id": observation.id, "media_type": "image"},
                                 files={"file": ("shot.png", _png(), "image/png")})
    assert uploaded.status_code in (200, 201), uploaded.text
    media_id = uploaded.json()["media_file_id"]
    for route in (MediaRoutes.MEDIA_ARCHIVE, MediaRoutes.MEDIA_RESTORE):
        assert (await client.post(V1.path(route, media_id=media_id), headers=h)).status_code == 200
    shared = await client.post(V1.path(MediaRoutes.MEDIA_SHARE, media_id=media_id), headers=h,
                               json={"ttl_seconds": 600})
    assert shared.status_code == 200, shared.text
    assert _summary(await _rows(db)) == [
        _media(media_id, observation.id, post.id, "created"), _media(media_id, observation.id, post.id, "archived"),
        _media(media_id, observation.id, post.id, "restored")], "a share link changes nothing a page shows"


async def test_a_resumable_upload_publishes_once_at_its_completion(client, db, headers, storage, events_on):
    post, user, observation = await _media_scene(db)
    h = headers(user)
    data = _png()
    created = await client.post(V1.path(MediaRoutes.UPLOADS), headers=h, json={
        "observation_id": observation.id, "media_type": "image", "original_filename": "photo.png",
        "expected_size": len(data), "content_type": "image/png", "sha256": hashlib.sha256(data).hexdigest()})
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    session = V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id)
    chunk = {"Content-Type": "application/offset+octet-stream", **h}
    cut = len(data) // 2
    assert (await client.patch(session, content=data[:cut], headers={**chunk, "Upload-Offset": "0"})).status_code == 204
    assert (await client.patch(session, content=data[cut:],
                               headers={**chunk, "Upload-Offset": str(cut)})).status_code == 204
    assert await _rows(db) == [], "never upload progress"
    done = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id), headers=h)
    assert done.status_code == 200, done.text
    assert _summary(await _rows(db)) == [_media(done.json()["media_file_id"], observation.id, post.id, "created")]


async def test_an_aborted_upload_and_refused_media_writes_publish_nothing(client, db, headers, storage, events_on):
    post, user, observation = await _media_scene(db)
    viewer = await make_user(db, [P.MEDIA_VIEW, P.OBSERVATION_VIEW], post_id=post.id)
    media = await make_media(db, observation, user)
    created = await client.post(V1.path(MediaRoutes.UPLOADS), headers=headers(user), json={
        "observation_id": observation.id, "media_type": "image", "original_filename": "photo.png",
        "expected_size": 100, "content_type": "image/png"})
    assert created.status_code == 201, created.text
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=created.json()["id"]),
                                headers=headers(user))
    assert aborted.status_code == 200, aborted.text
    assert (await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media.id),
                              headers=headers(viewer))).status_code == 403
    assert (await client.post(V1.path(MediaRoutes.MEDIA_RESTORE, media_id=media.id),
                              headers=headers(user))).status_code == 409
    refused = await client.post(V1.path(MediaRoutes.UPLOAD), headers=headers(viewer),
                                data={"observation_id": observation.id, "media_type": "image"},
                                files={"file": ("shot.png", _png(), "image/png")})
    assert refused.status_code == 403, refused.text
    assert await _rows(db) == []


async def test_media_of_old_data_without_an_observation_publishes_nothing(client, db, headers, events_on):
    """Owner, 2026-11-26: work media always has an observation; nothing on screen follows the others. The archive
    still passes the audit guard (events_follow_audit): no event is the decision of this write"""
    post = await make_post(db)
    ids = await make_dictionary(db)
    admin = await make_admin(db)
    media = await make_media(db, await make_observation(db, admin, post, ids["signal_type_id"]), admin)
    media.observation_id = None
    db.add(media)
    await db.commit()
    done = await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media.id), headers=headers(admin))
    assert done.status_code == 200, done.text
    assert await _rows(db) == []


async def test_the_observation_of_a_media_event_is_required(db, events_on):
    from app.backend.services.events import publish
    for observation in ({"observation_id": "4"}, {"observation_id": None}, {}):
        with pytest.raises(TypeError):
            publish(db, EventType.MEDIA_CHANGED, scope=Node.post(1), permission=P.MEDIA_VIEW, media_id=1,
                    media_change="created", **observation)
    with pytest.raises(TypeError):
        publish(db, EventType.OBSERVATION_CHANGED, scope=Node.post(1), permission=P.OBSERVATION_VIEW,
                observation_id=None, observation_change="created")
    await db.commit()
    assert await _rows(db) == []


# ---------------------------------------------------------------- media.changed from the worker

async def _videos(db, count: int, **fields):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, observation, [await make_media(db, observation, user, media_type="video", **fields)
                               for _ in range(count)]


async def test_poster_outcomes_a_page_shows_publish_processed(db, events_on, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "max_attempts", 2)
    post, observation, (ready, retried, silent) = await _videos(db, 3)
    assert (await jobs.claim_next_poster(db, "w1")).id == ready.id
    assert await jobs.complete_poster(db, ready.id, "w1") is True
    assert await jobs.complete_poster(db, ready.id, "w1") is False, "lost: nothing more"
    assert (await jobs.claim_next_poster(db, "w1")).id == retried.id
    assert await jobs.fail_poster(db, retried.id, "w1", "boom") == "pending", "pending again: a page sees nothing new"
    assert (await jobs.claim_next_poster(db, "w1")).id == silent.id
    assert await jobs.mark_poster_unsupported(db, silent.id, worker="w1") is True
    later_on = datetime.now(timezone.utc) + timedelta(hours=1)
    assert (await jobs.claim_next_poster(db, "w1", now=later_on)).id == retried.id
    assert await jobs.fail_poster(db, retried.id, "w1", "boom") == "failed"
    assert _summary(await _rows(db)) == [_media(ready.id, observation.id, post.id, "processed"),
                                         _media(silent.id, observation.id, post.id, "processed"),
                                         _media(retried.id, observation.id, post.id, "processed")]


async def test_a_poster_of_media_without_an_observation_publishes_nothing(db, events_on):
    post, observation, (video,) = await _videos(db, 1)
    video.observation_id = None
    db.add(video)
    await db.commit()
    assert (await jobs.claim_next_poster(db, "w1")).id == video.id
    assert await jobs.complete_poster(db, video.id, "w1") is True
    assert await _rows(db) == []


async def test_an_upload_found_without_video_publishes_only_its_creation(db, events_on):
    post, observation, (video,) = await _videos(db, 1)
    assert await jobs.mark_poster_unsupported(db, video.id) is True, "the upload's probe: no worker"
    assert await _rows(db) == [], "record_upload publishes created in that transaction"


async def test_an_abandoned_last_attempt_publishes_processed(db, events_on, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "max_attempts", 1)
    post, observation, (gone, other) = await _videos(db, 2)
    start = datetime.now(timezone.utc)
    await jobs.claim_next_poster(db, "w1", lease_seconds=300, now=start)
    assert await jobs.fail_exhausted_posters(db, now=start + timedelta(seconds=100)) == 0
    assert await _rows(db) == []
    assert await jobs.fail_exhausted_posters(db, now=start + timedelta(seconds=301)) == 1
    assert _summary(await _rows(db)) == [_media(gone.id, observation.id, post.id, "processed")]


async def test_the_worker_publishes_processed_when_a_poster_is_ready(db, session_maker, tmp_path, events_on):
    post, observation, (video,) = await _videos(db, 1)

    def extractor(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg")

    worker = MediaWorker(session_maker, tmp_path, name="media@test:1", extractor=extractor)
    assert await worker.claim_and_run_once() is True
    assert _summary(await _rows(db)) == [_media(video.id, observation.id, post.id, "processed")]


async def test_the_worker_publishes_nothing_for_a_failure_it_retries(db, session_maker, tmp_path, events_on):
    post, observation, (video,) = await _videos(db, 1)

    def extractor(source: Path, target: Path) -> None:
        raise PosterError("no frame")

    worker = MediaWorker(session_maker, tmp_path, name="media@test:1", extractor=extractor)
    assert await worker.claim_and_run_once() is True
    assert await _rows(db) == []


@pytest.fixture
def storyboard_settings(monkeypatch):
    config = settings.storage.storyboard
    monkeypatch.setattr(config, "enabled", True)
    monkeypatch.setattr(config, "max_attempts", 2)
    monkeypatch.setattr(config, "backoff_seconds", [60])
    return config


async def test_storyboard_outcomes_publish_processed(db, events_on, storyboard_settings):
    post, observation, (newer_first, retried) = await _videos(db, 2, poster_status="ready", duration_seconds=120)
    # the newest video is claimed first
    for media in (newer_first, retried):
        db.add(VideoStoryboard(media_id=media.id, status="pending"))
    await db.commit()
    plan = StoryboardPlan(step_seconds=2, frame_count=30, columns=10, rows=10, tile_width=160, sheet_count=1,
                          duration_seconds=60)
    assert (await storyboard_jobs.claim_next_storyboard(db, "w1")).media_id == retried.id
    assert await storyboard_jobs.fail_storyboard(db, retried.id, "w1", "boom") == "pending"
    assert (await storyboard_jobs.claim_next_storyboard(db, "w1")).media_id == newer_first.id
    assert await storyboard_jobs.complete_storyboard(db, newer_first.id, "w1", plan,
                                                     StoryboardResult(tile_height=90, sheet_count=1)) is True
    far = datetime.now(timezone.utc) + timedelta(hours=1)
    assert (await storyboard_jobs.claim_next_storyboard(db, "w1", now=far)).media_id == retried.id
    assert await storyboard_jobs.fail_storyboard(db, retried.id, "w1", "boom") == "failed"
    assert _summary(await _rows(db)) == [_media(newer_first.id, observation.id, post.id, "processed"),
                                         _media(retried.id, observation.id, post.id, "processed")]


async def test_an_abandoned_storyboard_publishes_processed(db, events_on, storyboard_settings):
    storyboard_settings.max_attempts = 1
    post, observation, (video,) = await _videos(db, 1, poster_status="ready", duration_seconds=120)
    db.add(VideoStoryboard(media_id=video.id, status="pending"))
    await db.commit()
    start = datetime.now(timezone.utc)
    await storyboard_jobs.claim_next_storyboard(db, "w1", lease_seconds=300, now=start)
    assert await storyboard_jobs.fail_exhausted_storyboards(db, now=start + timedelta(seconds=301)) == 1
    assert _summary(await _rows(db)) == [_media(video.id, observation.id, post.id, "processed")]


# ---------------------------------------------------------------- who receives them

async def _hub(engine) -> EventHub:
    hub = EventHub(engine, TextRenderer(), EventsConfig(enabled=True), workers=1)
    await hub.start(run_tasks=False)
    return hub


async def _attach(hub, db, user):
    return hub.attach(await load_auth_context(db, user, session_id=f"s{user.id}"), later(), None)


async def test_data_events_reach_the_holders_at_the_post_and_above(client, db, engine, headers, storage, events_on):
    group = await make_group(db)
    post_a, post_b = await make_post(db, group_id=group.id), await make_post(db, group_id=group.id)
    ids = await make_dictionary(db)
    writer_perms = [P.OBSERVATION_VIEW, P.OBSERVATION_CREATE, P.MEDIA_VIEW, P.MEDIA_CREATE]
    writer_a = await make_user(db, writer_perms, post_id=post_a.id)
    writer_b = await make_user(db, writer_perms, post_id=post_b.id)
    observations_a = await make_user(db, [P.OBSERVATION_VIEW], post_id=post_a.id)
    media_a = await make_user(db, [P.MEDIA_VIEW], post_id=post_a.id)
    at_group = await make_user(db, user_type=UserType.ANALYST)
    await give(db, at_group, [P.OBSERVATION_VIEW, P.MEDIA_VIEW], Node.group(group.id))
    nobody = await make_user(db, [P.POST_VIEW], post_id=post_a.id)
    hub = await _hub(engine)
    streams = {name: await _attach(hub, db, user) for name, user in (
        ("observations_a", observations_a), ("media_a", media_a), ("at_group", at_group), ("nobody", nobody))}

    seen = {}
    for name, writer, post in (("a", writer_a, post_a), ("b", writer_b, post_b)):
        created = await client.post(V1.path(ObservationRoutes.CREATE), headers=headers(writer),
                                    json=observation_payload(post.id, ids["signal_type_id"]))
        assert created.status_code == 200, created.text
        obs_id = created.json()["id"]
        uploaded = await client.post(V1.path(MediaRoutes.UPLOAD), headers=headers(writer),
                                     data={"observation_id": obs_id, "media_type": "image"},
                                     files={"file": (f"{name}.png", _png(), "image/png")})
        assert uploaded.status_code in (200, 201), uploaded.text
        seen[name] = (("observation.changed", {"observation_id": obs_id, "observation_change": "created"}),
                      ("media.changed", {"media_id": uploaded.json()["media_file_id"], "observation_id": obs_id,
                                         "media_change": "created"}))
    assert await hub.tick() == 4
    received = {name: [(event, {k: v for k, v in data.items() if k not in ("id", "refetch_within_ms")})
                       for _, _, event, data in queued(stream)[1:]] for name, stream in streams.items()}
    assert received == {
        "observations_a": [seen["a"][0]],
        "media_a": [seen["a"][1]],
        "at_group": [seen["a"][0], seen["a"][1], seen["b"][0], seen["b"][1]],
        "nobody": [],
    }


async def test_a_burst_on_one_observation_waits_in_a_queue_as_one_event(client, db, engine, headers, events_on):
    post, ids, user = await _observation_scene(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    hub = await _hub(engine)
    watcher = await _attach(hub, db, user)
    by_id = V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=observation.id)
    for n in range(3):
        assert (await client.put(by_id, headers=headers(user), json={"comment": f"c{n}"})).status_code == 200
    assert await hub.tick() == 3
    assert [event for _, _, event, _ in queued(watcher)] == ["ready", "observation.changed"]
