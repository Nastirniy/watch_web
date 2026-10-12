"""Post archiving: the record routes, the data scope (an archived post leaves every home-post and access-row scope;
post.access_all still reads it), the write refusal (post_archived) and migration 020."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.general import Permissions as P
from app.backend.enums.routes import (
    AuthRoutes, MediaRoutes, ObservationRoutes, PostRoutes, SearchRoutes, StatisticsRoutes, UserRoutes,
)
from app.backend.models.audit import AuditLog
from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.post import Post
from app.backend.models.user import User
from app.backend.models.role_assignment import RoleAssignment
from tests.helpers import (
    ACCESS_ALL, ADMIN_TRUE, make_admin, make_dictionary, make_media, make_observation, make_post, make_user, observation_payload,
)
from tests.test_column_bounds import _run_downgrade
from tests.test_media_uploads import CHUNK, _create_body, make_png, storage  # noqa: F401
from tests.test_migrations import HEAD, run_migrate
from tests.test_upload_filenames_and_temp import _nginx_post, _temp_file, temp_dir  # noqa: F401
from tests.test_username_validation import make_certificate

# Everything a post member does with the data of a post
DATA_PERMS = [
    P.POST_VIEW, P.STATISTICS_VIEW,
    P.OBSERVATION_VIEW, P.OBSERVATION_CREATE, P.OBSERVATION_UPDATE, P.OBSERVATION_ARCHIVE,
    P.MEDIA_VIEW, P.MEDIA_CREATE, P.MEDIA_ARCHIVE,
]
ACCESS_ALL_PERMS = DATA_PERMS + [ACCESS_ALL, P.OBSERVATION_MANAGE_ANY, P.MEDIA_MANAGE_ANY]

OBSERVATIONS = V1.path(ObservationRoutes.LIST_ALL)
ME = V1.path(AuthRoutes.ME)
STATISTICS = V1.path(StatisticsRoutes.ROOT)


@dataclass
class Scene:
    post: Post            # the post the tests archive
    other: Post
    home: User            # home post = post
    reader: User          # home post = other, a legacy access row (can_write) for post: assignments at both
    access_all: User      # post.access_all, not an administrator
    admin: User           # the seeded admin role: every permission, post.access_all included
    observation: Observation
    media: MediaFile
    signal_type_id: int
    tag_id: int


async def _scene(db) -> Scene:
    post, other = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    home = await make_user(db, perms=DATA_PERMS, post_id=post.id)
    reader = await make_user(db, perms=DATA_PERMS, post_id=other.id, extra_access={post.id: True})
    access_all = await make_user(db, perms=ACCESS_ALL_PERMS)
    admin = await make_admin(db)
    observation = await make_observation(db, home, post, ids["signal_type_id"])
    media = await make_media(db, observation, home)
    return Scene(post, other, home, reader, access_all, admin, observation, media, ids["signal_type_id"], ids["tag_id"])


async def _set_archived(db, post: Post, archived: bool = True) -> None:
    post.is_deleted = archived
    db.add(post)
    await db.commit()


async def _observation_ids(client, headers, user, **params) -> list:
    """The observations the user lists; none when the user holds observation.view nowhere (403 permission_denied:
    a grant only on an archived post applies nowhere, R39)"""
    response = await client.get(OBSERVATIONS, params=params, headers=headers(user))
    if response.status_code == 403:
        assert response.json()["code"] == "permission_denied", response.text
        return []
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()]


async def _reads_the_data(client, headers, scene: Scene, user) -> bool:
    """Lists the observation and opens its media; False when the post is out of the user's data scope"""
    listed = scene.observation.id in await _observation_ids(client, headers, user)
    media = await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=scene.media.id), headers=headers(user))
    filtered = await client.get(OBSERVATIONS, params={"post_id": scene.post.id}, headers=headers(user))
    assert media.status_code in (200, 403) and filtered.status_code in (200, 403), (media.text, filtered.text)
    assert listed == (media.status_code == 200) == (filtered.status_code == 200), (listed, media.text, filtered.text)
    return listed


# ----------------------------------------------------------------- post records
POSTS = V1.path(PostRoutes.ROOT)
SEARCH_POSTS = V1.path(SearchRoutes.POSTS)


def _archive_path(post_id: int) -> str:
    return V1.path(PostRoutes.POST_ARCHIVE, post_id=post_id)


def _restore_path(post_id: int) -> str:
    return V1.path(PostRoutes.POST_RESTORE, post_id=post_id)


async def _audit_actions(db, post_id: int) -> list:
    rows = (await db.exec(
        select(AuditLog).where(AuditLog.resource_type == "post", AuditLog.resource_id == post_id).order_by(AuditLog.id)
    )).all()
    return [row.action for row in rows]


@pytest.mark.asyncio
async def test_archive_and_restore_a_post(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    post_id = post.id

    archived = await client.post(_archive_path(post_id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    assert archived.json()["id"] == post_id and archived.json()["is_deleted"] is True
    again = await client.post(_archive_path(post_id), headers=headers(admin))
    assert again.status_code == 404 and again.json()["code"] == "post_not_found", again.text

    restored = await client.post(_restore_path(post_id), headers=headers(admin))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False, restored.text
    live = await client.post(_restore_path(post_id), headers=headers(admin))
    assert live.status_code == 409 and live.json()["code"] == "post_not_archived", live.text

    for path in (_archive_path(999999), _restore_path(999999)):
        unknown = await client.post(path, headers=headers(admin))
        assert unknown.status_code == 404 and unknown.json()["code"] == "post_not_found", unknown.text
    assert await _audit_actions(db, post_id) == ["archive", "restore"]


@pytest.mark.asyncio
async def test_archive_and_restore_need_post_view_and_post_archive(client, db, headers):
    """No admin gate; post.archive of a role without admin.true was dead behind the gate and still counts nowhere"""
    post = await make_post(db)
    not_admin = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE, ACCESS_ALL])
    without_archive = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW, P.POST_UPDATE])
    for path in (_archive_path(post.id), _restore_path(post.id)):
        for user in (not_admin, without_archive):
            refused = await client.post(path, headers=headers(user))
            assert refused.status_code == 403 and refused.json()["code"] == "permission_denied", refused.text
            assert refused.json()["missing"] == ["post.archive"]
    await db.refresh(post)
    assert post.is_deleted is False


@pytest.mark.asyncio
async def test_an_archived_post_answers_404_on_update_and_is_deleted_is_never_read_from_a_body(client, db, headers):
    admin = await make_admin(db)
    live, gone = await make_post(db), await make_post(db)
    await _set_archived(db, gone)

    response = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=gone.id), headers=headers(admin), json={"name": "New"})
    assert response.status_code == 404 and response.json()["code"] == "post_not_found", response.text

    response = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=live.id), headers=headers(admin), json={"is_deleted": True})
    assert response.status_code == 200 and response.json()["is_deleted"] is False, response.text
    response = await client.post(POSTS, headers=headers(admin), json={"name": "Born archived?", "code": "BORN-1", "is_deleted": True})
    assert response.status_code == 200 and response.json()["is_deleted"] is False, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [POSTS, SEARCH_POSTS])
async def test_lists_hide_archived_posts_unless_include_deleted(client, db, headers, path):
    live, gone = await make_post(db), await make_post(db)
    await _set_archived(db, gone)
    manager = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW, P.POST_ARCHIVE])
    viewer = await make_user(db, perms=[P.POST_VIEW, ACCESS_ALL])
    # post.archive without admin.true is held nowhere (it was dead behind the gate)
    dead_archive = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE, ACCESS_ALL])

    def ids(response) -> list:
        assert response.status_code == 200, response.text
        body = response.json()
        return [item["id"] for item in (body["items"] if isinstance(body, dict) else body)]

    assert ids(await client.get(path, headers=headers(viewer))) == [live.id]
    for user in (viewer, dead_archive):
        refused = await client.get(path, params={"include_deleted": "true"}, headers=headers(user))
        assert refused.status_code == 403 and refused.json()["code"] == "permission_required_for_action", refused.text
    assert ids(await client.get(path, headers=headers(manager))) == [live.id]
    assert ids(await client.get(path, params={"include_deleted": "true"}, headers=headers(manager))) == [live.id, gone.id]


@pytest.mark.asyncio
async def test_get_an_archived_post(client, db, headers):
    """404 unless post.archive is in effect at the post (an administrator without it no longer sees it); a home user
    is out of scope (a grant on the archived post itself stops applying)"""
    post = await make_post(db)
    home = await make_user(db, perms=[P.POST_VIEW, P.POST_ARCHIVE], post_id=post.id)
    viewer = await make_user(db, perms=[P.POST_VIEW, ACCESS_ALL])
    manager = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW, P.POST_ARCHIVE])
    admin = await make_user(db, perms=[ADMIN_TRUE, P.POST_VIEW])
    path = V1.path(PostRoutes.POST_BY_ID, post_id=post.id)
    for user in (home, viewer, manager, admin):
        assert (await client.get(path, headers=headers(user))).status_code == 200

    await _set_archived(db, post)
    assert (await client.get(path, headers=headers(home))).status_code == 403
    for user in (viewer, admin):
        hidden = await client.get(path, headers=headers(user))
        assert hidden.status_code == 404 and hidden.json()["code"] == "post_not_found", hidden.text
    shown = await client.get(path, headers=headers(manager))
    assert shown.status_code == 200 and shown.json()["is_deleted"] is True, shown.text


# ----------------------------------------------------------------- data scope
@pytest.mark.asyncio
async def test_home_and_access_row_users_lose_the_data_and_get_it_back_after_restore(client, db, headers):
    scene = await _scene(db)
    for user in (scene.home, scene.reader):
        assert await _reads_the_data(client, headers, scene, user)

    assert (await client.post(_archive_path(scene.post.id), headers=headers(scene.admin))).status_code == 200
    for user in (scene.home, scene.reader):
        assert not await _reads_the_data(client, headers, scene, user)
        me = (await client.get(ME, headers=headers(user))).json()
        assert str(scene.post.id) not in me["post_permissions"]
    # The access row is kept; the reader still reads its home post
    assert set((await client.get(ME, headers=headers(scene.reader))).json()["post_permissions"]) == {str(scene.other.id)}
    assert (await db.exec(select(RoleAssignment).where(RoleAssignment.scope_post_id == scene.post.id))).all() != []

    assert (await client.post(_restore_path(scene.post.id), headers=headers(scene.admin))).status_code == 200
    for user in (scene.home, scene.reader):
        assert await _reads_the_data(client, headers, scene, user)


@pytest.mark.asyncio
async def test_access_all_still_reads_the_data_of_an_archived_post(client, db, headers):
    scene = await _scene(db)
    await _set_archived(db, scene.post)
    assert await _reads_the_data(client, headers, scene, scene.access_all)
    assert await _reads_the_data(client, headers, scene, scene.admin)


@pytest.mark.asyncio
async def test_an_administrator_without_access_all_loses_the_data_too(client, db, headers):
    """Observations, media and reports never get the administrator exception"""
    scene = await _scene(db)
    admin = await make_user(db, perms=DATA_PERMS + [ADMIN_TRUE], post_id=scene.post.id)
    assert await _reads_the_data(client, headers, scene, admin)
    await _set_archived(db, scene.post)
    assert not await _reads_the_data(client, headers, scene, admin)


@pytest.mark.asyncio
async def test_statistics_post_counts_leave_out_archived_posts(client, db, headers):
    scene = await _scene(db)
    before = (await client.get(STATISTICS, headers=headers(scene.admin))).json()["posts"]
    await _set_archived(db, scene.post)
    after = (await client.get(STATISTICS, headers=headers(scene.admin))).json()["posts"]
    assert after["total"] == before["total"] - 1, (before, after)
    assert after["active"] == before["active"] - 1, (before, after)
    access_all = (await client.get(STATISTICS, headers=headers(scene.access_all))).json()["posts"]
    assert access_all["total"] == after["total"]


@pytest.mark.asyncio
async def test_statistics_and_observation_stats_follow_the_scope(client, db, headers):
    scene = await _scene(db)
    stats = V1.path(PostRoutes.POST_OBSERVATION_STATS, post_id=scene.post.id)
    assert (await client.get(STATISTICS, headers=headers(scene.reader))).json()["post_ids"] == sorted([scene.post.id, scene.other.id])

    await _set_archived(db, scene.post)
    assert (await client.get(STATISTICS, headers=headers(scene.reader))).json()["post_ids"] == [scene.other.id]
    # Its only post archived, the home user holds statistics.view nowhere (R39; before: an empty list of posts)
    home = await client.get(STATISTICS, headers=headers(scene.home))
    assert home.status_code == 403 and home.json()["missing"] == ["statistics.view"], home.text
    refused = await client.get(stats, headers=headers(scene.home))
    assert refused.status_code == 403, refused.text
    # Counters are aggregates: administrators count any post, post.access_all reads every post
    for user in (scene.admin, scene.access_all):
        response = await client.get(stats, headers=headers(user))
        assert response.status_code == 200, response.text
        assert response.json()["all_time"]["not_archived"] == 1


# ----------------------------------------------------------------- writes into an archived post
def _observation_writes(scene: Scene) -> dict:
    """Every write into the data of a post: name -> (method, path, request kwargs)"""
    obs = {"observation_id": scene.observation.id}
    band = {**obs, "frequency_id": scene.observation.all_frequencies[0].id}
    return {
        "create": ("POST", V1.path(ObservationRoutes.CREATE), {"json": observation_payload(scene.post.id, scene.signal_type_id)}),
        "update": ("PUT", V1.path(ObservationRoutes.OBSERVATION_BY_ID, **obs), {"json": {"comment": "changed"}}),
        "tags": ("PUT", V1.path(ObservationRoutes.OBSERVATION_BY_ID, **obs), {"json": {"tag_ids": [scene.tag_id]}}),
        "disappeared": ("POST", V1.path(ObservationRoutes.MARK_DISAPPEARED, **obs),
                        {"json": {"disappeared_at": datetime.now(timezone.utc).isoformat()}}),
        "band_add": ("POST", V1.path(ObservationRoutes.OBSERVATION_FREQUENCIES, **obs),
                     {"json": {"kind": "rc", "frequency_min": "300.000", "frequency_max": "400.000"}}),
        "band_update": ("PUT", V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID, **band), {"json": {"frequency_max": "250.000"}}),
        "band_archive": ("POST", V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE, **band), {}),
        "band_restore": ("POST", V1.path(ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE, **band), {}),
        "media_archive": ("POST", V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=scene.media.id), {}),
        "media_restore": ("POST", V1.path(MediaRoutes.MEDIA_RESTORE, media_id=scene.media.id), {}),
        "upload": ("POST", V1.path(MediaRoutes.UPLOAD), {
            "data": {"observation_id": str(scene.observation.id), "media_type": "image"},
            "files": {"file": ("photo.png", make_png(), "image/png")}}),
        "resumable_create": ("POST", V1.path(MediaRoutes.UPLOADS), {"json": _create_body(scene.observation.id, make_png())}),
    }


WRITES = ["create", "update", "tags", "disappeared", "band_add", "band_update", "band_archive", "band_restore",
          "media_archive", "media_restore", "upload", "resumable_create"]


async def _load_scene(db) -> Scene:
    """The scene with the bands of the observation loaded"""
    scene = await _scene(db)
    scene.observation = (await db.exec(
        select(Observation).where(Observation.id == scene.observation.id).options(selectinload(Observation.all_frequencies))
    )).one()
    return scene


def _is_post_archived(response, post: Post) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "post_archived" and response.json()["post_id"] == post.id, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITES)
async def test_writes_into_an_archived_post_answer_post_archived(client, db, headers, storage, write):
    scene = await _load_scene(db)
    method, path, kwargs = _observation_writes(scene)[write]
    await _set_archived(db, scene.post)

    _is_post_archived(await client.request(method, path, headers=headers(scene.access_all), **kwargs), scene.post)
    # The scope first: a home user of the archived post is an outsider now and gets the 403
    outsider = await client.request(method, path, headers=headers(scene.home), **_observation_writes(scene)[write][2])
    assert outsider.status_code == 403, outsider.text

    # Nothing changed
    observation_id = scene.observation.id
    db.expire_all()
    observation = await db.get(Observation, observation_id)
    assert observation.comment is None and observation.status == "active" and not observation.is_deleted
    assert (await db.exec(select(func.count()).select_from(MediaFile))).one() == 1
    assert (await db.exec(select(func.count()).select_from(Observation))).one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("write", ["create", "band_add", "media_archive", "upload", "resumable_create"])
async def test_the_same_writes_work_again_after_restore(client, db, headers, storage, write):
    scene = await _load_scene(db)
    method, path, kwargs = _observation_writes(scene)[write]
    await _set_archived(db, scene.post)
    await _set_archived(db, scene.post, False)
    response = await client.request(method, path, headers=headers(scene.access_all), **kwargs)
    assert response.status_code in (200, 201), response.text


@pytest.mark.asyncio
async def test_nginx_upload_into_an_archived_post_answers_post_archived(client, db, headers, storage, temp_dir):
    scene = await _scene(db)
    await _set_archived(db, scene.post)
    body = _temp_file(temp_dir)
    _is_post_archived(await _nginx_post(client, headers, scene.access_all, scene.observation, body), scene.post)
    assert not body.exists(), "the refused body file is removed"


@pytest.mark.asyncio
async def test_completing_an_upload_into_an_archived_post_answers_post_archived(client, db, headers, storage):
    scene = await _scene(db)
    data = make_png()
    created = await client.post(V1.path(MediaRoutes.UPLOADS), json=_create_body(scene.observation.id, data), headers=headers(scene.access_all))
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    session_path = V1.path(MediaRoutes.UPLOAD_SESSION, upload_id=upload_id)
    chunk = await client.patch(session_path, content=data, headers={**CHUNK, "Upload-Offset": "0", **headers(scene.access_all)})
    assert chunk.status_code == 204, chunk.text

    await _set_archived(db, scene.post)
    complete = V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id)
    _is_post_archived(await client.post(complete, headers=headers(scene.access_all)), scene.post)
    state = await client.get(session_path, headers=headers(scene.access_all))
    assert state.json()["status"] == "open", "the session waits: it completes after a restore"

    await _set_archived(db, scene.post, False)
    done = await client.post(complete, headers=headers(scene.access_all))
    assert done.status_code == 200, done.text


@pytest.mark.asyncio
async def test_a_closed_upload_session_of_an_archived_post_answers_410_not_post_archived(client, db, headers, storage):
    scene = await _scene(db)
    data = make_png()
    created = await client.post(V1.path(MediaRoutes.UPLOADS), json=_create_body(scene.observation.id, data), headers=headers(scene.access_all))
    upload_id = created.json()["id"]
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=upload_id), headers=headers(scene.access_all))
    assert aborted.status_code == 200, aborted.text

    await _set_archived(db, scene.post)
    complete = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_COMPLETE, upload_id=upload_id), headers=headers(scene.access_all))
    assert complete.status_code == 410 and complete.json()["code"] == "upload_session_closed", complete.text


@pytest.mark.asyncio
async def test_poster_regenerate_in_an_archived_post_answers_post_archived(client, db, headers, storage):
    scene = await _scene(db)
    video = await make_media(db, scene.observation, scene.home, "video")
    await _set_archived(db, scene.post)
    response = await client.post(V1.path(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE, media_id=video.id), headers=headers(scene.admin))
    _is_post_archived(response, scene.post)


@pytest.mark.asyncio
async def test_administrators_archive_and_restore_observations_of_an_archived_post(client, db, headers):
    """post.archive in effect at the archived post (here the built-in admin role at the root) with observation.view
    and observation.archive; post.access_all without post.archive gets post_archived, a home-post user 403"""
    scene = await _scene(db)
    await _set_archived(db, scene.post)
    archive = V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=scene.observation.id)
    restore = V1.path(ObservationRoutes.OBSERVATION_RESTORE, observation_id=scene.observation.id)

    _is_post_archived(await client.post(archive, headers=headers(scene.access_all)), scene.post)
    assert (await client.post(archive, headers=headers(scene.home))).status_code == 403

    archived = await client.post(archive, headers=headers(scene.admin))
    assert archived.status_code == 200 and archived.json()["is_deleted"] is True, archived.text
    _is_post_archived(await client.post(restore, headers=headers(scene.access_all)), scene.post)
    restored = await client.post(restore, headers=headers(scene.admin))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False, restored.text


# ----------------------------------------------------------------- home post and access rows
@pytest.mark.asyncio
async def test_an_archived_post_cannot_be_assigned(client, db, headers):
    scene = await _scene(db)
    await _set_archived(db, scene.post)
    admin, target = scene.admin, scene.access_all

    created = await client.post(
        V1.path(UserRoutes.ROOT), headers=headers(admin),
        files={"certificate": ("cert.pem", make_certificate("cn-archived-post"), "application/x-pem-file")},
        data={"username": "on_archived_post", "username_alias": "Alias", "post_id": str(scene.post.id),
              "user_type": "viewer"},
    )
    _is_post_archived(created, scene.post)

    moved = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(admin), data={"post_id": str(scene.post.id)})
    _is_post_archived(moved, scene.post)
    target_id = target.id
    db.expire_all()
    assert (await db.get(User, target_id)).post_id is None


@pytest.mark.asyncio
async def test_existing_assignments_of_an_archived_post_stay(client, db, headers):
    """Archiving changes no user row: the home post stays (a form that sends it again is no new assignment), and so
    do the assignments made on the post"""
    scene = await _scene(db)
    await _set_archived(db, scene.post)
    kept = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=scene.home.id), headers=headers(scene.admin),
                              data={"post_id": str(scene.post.id), "username_alias": "Still here"})
    assert kept.status_code == 200 and kept.json()["post_id"] == scene.post.id, kept.text
    moved = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=scene.home.id), headers=headers(scene.admin),
                               data={"post_id": str(scene.other.id)})
    assert moved.status_code == 200, moved.text
    assert (await db.exec(select(RoleAssignment).where(RoleAssignment.scope_post_id == scene.post.id))).all() != []


# ----------------------------------------------------------------- migration
def _is_deleted_column(conn):
    """(type, not null, default) of posts.is_deleted and the names of the indexes on it"""
    columns = {row[1]: row for row in conn.execute("pragma table_info(posts)")}
    if "is_deleted" not in columns:
        return None, set()
    _, _, column_type, not_null, default, _ = columns["is_deleted"]
    indexes = {name for _, name, *_ in conn.execute("pragma index_list(posts)")
               if [row[2] for row in conn.execute(f"pragma index_info({name})")] == ["is_deleted"]}
    return (column_type, not_null, default), indexes


@pytest.mark.slow
def test_020_adds_posts_is_deleted_like_a_fresh_install_and_downgrades(tmp_path):
    db_file = tmp_path / "posts.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    conn = sqlite3.connect(db_file)
    fresh, fresh_indexes = _is_deleted_column(conn)
    assert fresh is not None and fresh[1] == 1, "NOT NULL on a fresh install (built from the model)"
    assert fresh_indexes == {"ix_posts_is_deleted"}

    # A database at 019: no column, one post
    conn.execute("drop index ix_posts_is_deleted")
    conn.execute("alter table posts drop column is_deleted")
    conn.execute("insert into posts (name, code, is_active, created_at, updated_at)"
                 " values ('Old', 'OLD', 1, '2026-01-01', '2026-01-01')")
    conn.execute("update alembic_version set version_num = '019_certificate_cn_hash_scheme'")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "adding column posts.is_deleted" in second.stdout + second.stderr
    conn = sqlite3.connect(db_file)
    assert _is_deleted_column(conn) == (fresh, fresh_indexes), "the migration and a fresh install agree"
    assert conn.execute("select code, is_deleted from posts").fetchall() == [("OLD", 0)]
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()

    # Run again: guarded, nothing to add
    third = run_migrate(db_file)
    assert third.returncode == 0 and "adding column posts.is_deleted" not in third.stdout + third.stderr

    down = _run_downgrade(db_file, "019_certificate_cn_hash_scheme")
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert _is_deleted_column(conn) == (None, set())
    assert conn.execute("select code from posts").fetchall() == [("OLD",)]
    conn.close()
