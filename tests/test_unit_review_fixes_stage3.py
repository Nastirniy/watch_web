"""Minor findings of the corps follow-ups: the filters need the unit argument, a detach of a unit with no active post
needs no post rights, one post.view scope per unit list."""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import ObservationReportType, Permissions
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes
from app.backend.filters.media_filter import MediaCriteria, MediaFilterService
from app.backend.filters.observation_filter import ObservationCriteria, ObservationFilterService
from app.backend.filters.observation_report_filter import ObservationReportCriteria, ObservationReportFilter
from app.backend.filters.post_filter import PostCriteria, PostFilterService
from app.backend.filters.user_filter import UserCriteria, UserFilterService
from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.post import Post
from app.backend.models.user import User
from tests.helpers import assign, make_group, make_post, make_role, make_user


def test_filters_need_the_unit_argument():
    calls = [
        lambda: MediaFilterService.apply_filters(select(MediaFile), MediaCriteria()),
        lambda: MediaFilterService.apply_filters_for_count(select(MediaFile), MediaCriteria()),
        lambda: ObservationFilterService.apply_filters(select(Observation), ObservationCriteria()),
        lambda: ObservationFilterService.apply_filters_for_count(select(Observation), ObservationCriteria()),
        lambda: ObservationReportFilter.build_query(ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS)),
        lambda: PostFilterService.apply_filters(select(Post), PostCriteria()),
        lambda: PostFilterService.apply_filters_for_count(select(Post), PostCriteria()),
        lambda: UserFilterService.apply_filters(select(User), UserCriteria()),
        lambda: UserFilterService.apply_filters_for_count(select(User), UserCriteria()),
    ]
    for call in calls:
        with pytest.raises(TypeError, match="unit_"):
            call()


async def _archiver(db):
    """scope_group.view and scope_group.archive at the root, nothing about posts"""
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]), ROOT)
    return user


async def test_archiving_an_empty_brigade_without_cascade_needs_no_post_update(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    user = await _archiver(db)
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id), headers=headers(user))
    assert r.status_code == 200 and r.json()["is_deleted"] is True, r.text


async def test_archiving_an_empty_corps_without_cascade_needs_no_post_update(client, db, headers):
    corps = await make_group(db, kind="corps")
    user = await _archiver(db)
    r = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=headers(user))
    assert r.status_code == 200 and r.json()["is_deleted"] is True, r.text


async def test_a_unit_with_only_an_archived_post_detaches_without_post_rights(client, db, headers):
    corps = await make_group(db, kind="corps")
    post = await make_post(db, group_id=corps.id)
    post.is_deleted = True
    db.add(post)
    await db.commit()
    user = await _archiver(db)
    r = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=headers(user))
    assert r.status_code == 200, r.text


async def test_a_unit_with_an_active_post_still_needs_post_rights_to_detach(client, db, headers):
    corps = await make_group(db, kind="corps")
    await make_post(db, group_id=corps.id)
    user = await _archiver(db)
    r = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action", r.text


async def test_the_detach_still_needs_scope_group_archive_at_the_parent(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), ROOT)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_ARCHIVE]), Node.group(other.id))
    r = await client.post(V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action", r.text


def _count_post_view_reads(monkeypatch):
    reads = []
    original = AuthContext.posts_with

    def counting(self, permission):
        reads.append(str(getattr(permission, "value", permission)))
        return original(self, permission)

    monkeypatch.setattr(AuthContext, "posts_with", counting)
    return reads


async def test_corps_list_reads_post_view_once(client, db, headers, monkeypatch):
    for _ in range(5):
        corps = await make_group(db, kind="corps")
        await make_post(db, group_id=corps.id)
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.POST_VIEW]), ROOT)
    reads = _count_post_view_reads(monkeypatch)
    r = await client.get(V1.path(CorpsRoutes.ROOT), headers=headers(user))
    assert r.status_code == 200 and len(r.json()) == 5 and {c["post_count"] for c in r.json()} == {1}, r.text
    assert reads.count(Permissions.POST_VIEW.value) == 1, reads


async def test_brigade_lists_read_post_view_once(client, db, headers, monkeypatch):
    corps = await make_group(db, kind="corps")
    for _ in range(4):
        brigade = await make_group(db, parent_id=corps.id, kind="brigade")
        await make_post(db, group_id=brigade.id)
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.POST_VIEW]), ROOT)
    reads = _count_post_view_reads(monkeypatch)
    for path in (V1.path(BrigadeRoutes.ROOT), V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=corps.id)):
        reads.clear()
        r = await client.get(path, headers=headers(user))
        assert r.status_code == 200 and len(r.json()) == 4 and {b["post_count"] for b in r.json()} == {1}, r.text
        assert reads.count(Permissions.POST_VIEW.value) == 1, (path, reads)
