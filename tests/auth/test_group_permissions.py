"""group_permissions of the authorization info (/auth/me, login): group id -> the permissions the assignments at
groups put in effect at that group (the grant's group and every group below it, archived descendants included, like
covers). Grants at the root and at posts are left out; a grant at an archived group reaches nothing."""
import pytest

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.config import settings
from app.backend.enums.general import AppEnvironment, Permissions, UserType
from app.backend.enums.routes import AuthRoutes
from tests.helpers import assign, make_group, make_post, make_role, make_user, make_certificate_row
from tests.pki.test_cn_hash_scheme import _dev_login
from tests.pki.test_pki_login import make_certificate

ME = V1.path(AuthRoutes.ME)
LOGIN = V1.path(AuthRoutes.LOGIN)

UNIT_VIEW_UPDATE = [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE]
EXPECTED = sorted(p.value for p in UNIT_VIEW_UPDATE)


async def _me(client, headers, user) -> dict:
    r = await client.get(ME, headers=headers(user))
    assert r.status_code == 200, r.text
    return r.json()["group_permissions"]


async def _corps_tree(db):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    sibling = await make_group(db, parent_id=corps.id, kind="brigade")
    other = await make_group(db, kind="corps")
    return corps, brigade, sibling, other


@pytest.mark.asyncio
async def test_corps_commander_gets_the_corps_and_everything_below(client, db, headers):
    corps, brigade, sibling, other = await _corps_tree(db)
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, UNIT_VIEW_UPDATE), Node.group(corps.id))
    assert await _me(client, headers, user) == {
        str(corps.id): EXPECTED, str(brigade.id): EXPECTED, str(sibling.id): EXPECTED}


@pytest.mark.asyncio
async def test_root_only_user_has_no_entries(client, db, headers):
    await _corps_tree(db)
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, UNIT_VIEW_UPDATE), ROOT)
    assert await _me(client, headers, user) == {}


@pytest.mark.asyncio
async def test_grant_at_an_archived_corps_gives_nothing(client, db, headers):
    corps, brigade, _, _ = await _corps_tree(db)
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, UNIT_VIEW_UPDATE), Node.group(corps.id))
    corps.is_deleted = True
    db.add(corps)
    await db.commit()
    assert await _me(client, headers, user) == {}


@pytest.mark.asyncio
async def test_grant_at_a_post_gives_nothing(client, db, headers):
    corps, _, _, _ = await _corps_tree(db)
    post = await make_post(db, group_id=corps.id)
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, UNIT_VIEW_UPDATE), Node.post(post.id))
    assert await _me(client, headers, user) == {}


@pytest.mark.asyncio
async def test_two_roles_at_the_corps_and_a_brigade_union_at_the_brigade(client, db, headers):
    corps, brigade, sibling, _ = await _corps_tree(db)
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), Node.group(corps.id))
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_VIEW]),
                 Node.group(brigade.id))
    seen = await _me(client, headers, user)
    view = Permissions.SCOPE_GROUP_VIEW.value
    assert seen[str(corps.id)] == [view]
    assert seen[str(brigade.id)] == sorted([view, Permissions.SCOPE_GROUP_UPDATE.value, Permissions.POST_VIEW.value])
    assert seen[str(sibling.id)] == [view]


@pytest.mark.asyncio
async def test_login_response_carries_the_same_field(client, db, monkeypatch):
    corps, brigade, _, _ = await _corps_tree(db)
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await make_role(db, UNIT_VIEW_UPDATE), Node.group(brigade.id))
    cert = make_certificate("Group-Perms", 0xD1)
    await make_certificate_row(db, user, cert=cert)
    monkeypatch.setattr(settings.app, "environment", AppEnvironment.DEVELOPMENT.value)
    r = await client.post(LOGIN, json=_dev_login(cert, "Group-Perms"))
    assert r.status_code == 200, r.text
    assert r.json()["group_permissions"] == {str(brigade.id): EXPECTED}
