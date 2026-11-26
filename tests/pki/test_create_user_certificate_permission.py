"""POST /users with a certificate file registers a login certificate: like POST /users/{user_id}/certificates/upload
it needs certificate.view and certificate.create in effect at the new user's home node (owner decisions 2026-10-29,
audit L9). Without a certificate nothing changes."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import BuiltinRole, Permissions, UserType
from app.backend.enums.routes import UserRoutes
from app.backend.models.user import User
from tests.helpers import _permission_names, builtin_role, give, make_post, make_user
from tests.pki_helpers import legacy_root, make_leaf, pem

CREATE_USER = V1.path(UserRoutes.ROOT)
CERTIFICATE = [Permissions.CERTIFICATE_VIEW.value, Permissions.CERTIFICATE_CREATE.value]


async def _manager(db):
    """A caller who may create viewers everywhere, without any certificate permission"""
    manager = await make_user(db, user_type=UserType.ADMINISTRATOR)
    viewer = await _permission_names(db, (await builtin_role(db, BuiltinRole.VIEWER)).id)
    await give(db, manager, sorted(viewer | {Permissions.USER_VIEW.value, Permissions.USER_CREATE.value}))
    return manager


async def _create(client, headers, caller, username, post_id=None, with_certificate=True):
    data = {"username": username, "username_alias": "Alias", "user_type": "viewer"}
    if post_id is not None:
        data["post_id"] = str(post_id)
    files = None
    if with_certificate:
        cert, _ = make_leaf(legacy_root(), label=f"cn-{username}")
        files = {"certificate": ("cert.pem", pem(cert), "application/x-pem-file")}
    return await client.post(CREATE_USER, headers=headers(caller), data=data, files=files)


async def _exists(db, username) -> bool:
    return (await db.exec(select(User.id).where(User.username == username))).first() is not None


async def test_a_certificate_needs_certificate_create_at_the_home_node(client, db, headers):
    manager = await _manager(db)
    post, other = await make_post(db), await make_post(db)

    refused = await _create(client, headers, manager, "no_cert_permission", post.id)
    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body["code"] == "permission_required_for_action" and body["missing"] == [CERTIFICATE[0]], body
    assert not await _exists(db, "no_cert_permission")

    # Held at another post only: still refused, nothing written
    await give(db, manager, CERTIFICATE, Node.post(other.id))
    refused = await _create(client, headers, manager, "elsewhere", post.id)
    assert refused.status_code == 403 and refused.json()["code"] == "permission_required_for_action", refused.text
    assert not await _exists(db, "elsewhere")

    await give(db, manager, CERTIFICATE, Node.post(post.id))
    created = await _create(client, headers, manager, "with_cert", post.id)
    assert created.status_code == 200, created.text


async def test_view_alone_is_not_enough(client, db, headers):
    manager = await _manager(db)
    await give(db, manager, [Permissions.CERTIFICATE_VIEW.value], ROOT)
    refused = await _create(client, headers, manager, "view_only")
    assert refused.status_code == 403, refused.text
    assert refused.json()["missing"] == [Permissions.CERTIFICATE_CREATE.value], refused.json()
    assert not await _exists(db, "view_only")


async def test_without_a_certificate_nothing_changes(client, db, headers):
    manager = await _manager(db)
    created = await _create(client, headers, manager, "plain", with_certificate=False)
    assert created.status_code == 200, created.text
