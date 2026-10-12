"""Media listing and scope: archived media and media of archived observations stay out of GET /media and
GET /search/media, include_deleted needs media.archive, has_thumbnail follows the render limit, and
regenerating a poster needs access to the post of the media."""
import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes, SearchRoutes
from app.backend.filters.media_filter import MediaFilterService, MediaCriteria
from app.backend.auth.scope import Node
from tests.helpers import assign, make_group, make_post, make_role, make_user, make_dictionary, make_observation, make_media

LIST = ["media.view"]


def _ids(response, search: bool):
    assert response.status_code == 200, response.text
    body = response.json()
    items = body["items"] if search else body
    return {item["id"] for item in items}


async def _scene(db, perms=LIST):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=perms, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, ids, user, observation


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_archived_media_is_left_out_unless_include_deleted(client, db, headers, search):
    _, _, user, observation = await _scene(db, perms=LIST + ["media.archive"])
    live = await make_media(db, observation, user)
    archived = await make_media(db, observation, user, is_deleted=True)
    url = V1.path(SearchRoutes.MEDIA if search else MediaRoutes.ROOT)

    assert _ids(await client.get(url, headers=headers(user)), search) == {live.id}
    both = await client.get(url, params={"include_deleted": True}, headers=headers(user))
    assert _ids(both, search) == {live.id, archived.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_include_deleted_needs_media_archive(client, db, headers, search):
    _, _, user, observation = await _scene(db)
    await make_media(db, observation, user, is_deleted=True)
    url = V1.path(SearchRoutes.MEDIA if search else MediaRoutes.ROOT)

    response = await client.get(url, params={"include_deleted": True}, headers=headers(user))
    assert response.status_code == 403
    assert response.json()["code"] == "permission_required_for_action"


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_media_of_an_archived_observation_is_never_listed(client, db, headers, search):
    """Reproduced: the list showed media (with token-free thumbnail URLs) that /media/{id}/info answers 404 for"""
    _, _, user, observation = await _scene(db, perms=LIST + ["media.archive"])
    media = await make_media(db, observation, user)
    url = V1.path(SearchRoutes.MEDIA if search else MediaRoutes.ROOT)
    assert _ids(await client.get(url, headers=headers(user)), search) == {media.id}

    observation.is_deleted = True
    db.add(observation)
    await db.commit()

    assert _ids(await client.get(url, headers=headers(user)), search) == set()
    still = await client.get(url, params={"include_deleted": True}, headers=headers(user))
    assert _ids(still, search) == set()


@pytest.mark.asyncio
async def test_search_total_counts_only_visible_media(client, db, headers):
    _, ids, user, observation = await _scene(db)
    await make_media(db, observation, user)
    await make_media(db, observation, user, is_deleted=True)
    gone = await make_observation(db, user, (await make_post(db)), ids["signal_type_id"])
    user_post = observation.post_id
    gone.post_id = user_post
    gone.is_deleted = True
    db.add(gone)
    await db.commit()
    await make_media(db, gone, user)

    response = await client.get(V1.path(SearchRoutes.MEDIA), headers=headers(user))
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1 and len(response.json()["items"]) == 1


@pytest.mark.asyncio
async def test_has_thumbnail_excludes_images_above_the_render_limit(client, db, headers):
    _, _, user, observation = await _scene(db)
    limit = settings.storage.imgproxy.max_src_resolution_mp * 1_000_000
    side = int(limit ** 0.5)
    small = await make_media(db, observation, user, width=100, height=100)
    unknown = await make_media(db, observation, user)
    exact = await make_media(db, observation, user, width=side, height=side)
    assert exact.width * exact.height <= limit and not exact.too_large_to_render
    huge = await make_media(db, observation, user, width=side + 10, height=side + 10)
    assert huge.too_large_to_render
    video = await make_media(db, observation, user, media_type="video", poster_status="ready",
                             width=side + 10, height=side + 10)
    assert not video.too_large_to_render
    pending = await make_media(db, observation, user, media_type="video")

    url = V1.path(MediaRoutes.ROOT)
    with_thumb = await client.get(url, params={"has_thumbnail": True}, headers=headers(user))
    assert _ids(with_thumb, False) == {small.id, unknown.id, exact.id, video.id}
    without = await client.get(url, params={"has_thumbnail": False}, headers=headers(user))
    assert _ids(without, False) == {huge.id, pending.id}

    # the SQL rule and the model property agree on every row
    for row in (small, unknown, exact, huge, video, pending):
        listed = row.id in _ids(with_thumb, False)
        assert listed == (row.thumbnail_status == "ready"), row.id


def test_media_criteria_carries_include_deleted():
    assert MediaCriteria().include_deleted is False
    assert "include_deleted" in MediaCriteria.__dataclass_fields__
    assert MediaFilterService.CRITERIA is MediaCriteria


@pytest.mark.asyncio
async def test_regenerate_needs_access_to_the_post_of_the_media(client, db, headers, tmp_path, monkeypatch):
    """Reproduced: an administrator of post A regenerated the poster of post B"""
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    post_b, _, owner_b, observation_b = await _scene(db)
    post_a = await make_post(db)
    admin_a = await make_user(db, perms=["media.view", "media.update", "admin.true"], post_id=post_a.id)
    video = await make_media(db, observation_b, owner_b, media_type="video", poster_status="ready")
    poster = video.resolve_poster_path(tmp_path)
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"jpeg")

    url = V1.path(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE, media_id=video.id)
    response = await client.post(url, headers=headers(admin_a))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "post_access_denied"
    assert poster.exists(), "a refused request must not touch the poster"
    await db.refresh(video)
    assert video.poster_status == "ready"

    admin_b = await make_user(db, perms=["media.view", "media.update", "admin.true"], post_id=post_b.id)
    ok = await client.post(url, headers=headers(admin_b))
    assert ok.status_code == 200, ok.text
    assert not poster.exists()


@pytest.mark.asyncio
async def test_regenerate_by_an_administrator_without_a_post_is_no_post_assigned(client, db, headers, tmp_path, monkeypatch):
    """check_post answers no_post_assigned to a caller whose media permissions apply at no post (held at a group
    without posts; a grant only on an archived post, or no grant, holds them nowhere and the route refuses the caller
    with permission_denied); the route declares it"""
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    _, _, owner, observation = await _scene(db)
    homeless_admin = await make_user(db)
    await assign(db, homeless_admin, await make_role(db, ["media.view", "media.update"]), Node.group((await make_group(db)).id))
    video = await make_media(db, observation, owner, media_type="video", poster_status="ready")

    response = await client.post(V1.path(MediaRoutes.MEDIA_THUMBNAIL_REGENERATE, media_id=video.id),
                                 headers=headers(homeless_admin))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "no_post_assigned"
    await db.refresh(video)
    assert video.poster_status == "ready"
