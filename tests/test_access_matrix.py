"""Table-driven access matrix over every guarded route.

For each route in the generated route access map and each persona, the call
must either pass the permission gate (status not 401/403) or be rejected with
a 403 carrying the expected code and missing list. Data scope is satisfied by
building every referenced resource inside the persona's own post. The expected
answer is the route's requirement evaluated on the persona's real AuthContext
(its role assignments, written by the legacy-shaped factory exactly as migration 023 converts them: a permission
that was dead without admin.true is held nowhere). There is no admin gate: admin.true alone gives nothing, every
refusal is permission_denied.
"""
import pytest
from sqlmodel import select

from app.main import fastapi_app
from app.backend.auth.route_access import route_access_map
from app.backend.auth.context import load_auth_context
from app.backend.auth.privileges import role_nodes
from app.backend.models.observation import ObservationFrequency
from app.backend.models.role import Permission
from app.backend.models.role_assignment import RoleAssignment
from tests.helpers import ADMIN_TRUE, make_post, make_user, make_dictionary, make_observation, make_media

ENTRIES = list(route_access_map(fastapi_app))
PERSONAS = ["no_perms", "granular", "granular_admin", "admin_only"]


def persona_permissions(entry, persona) -> list:
    granular = list(entry.require.all) + list(entry.require.any[:1])
    return {
        "no_perms": [],
        "granular": granular,
        "granular_admin": granular + [ADMIN_TRUE],
        "admin_only": [ADMIN_TRUE],
    }[persona]


async def expected(db, entry, user):
    return entry.require.evaluate(await load_auth_context(db, user))


async def grantable_permission(db, user, role_id: int, perms: list) -> int:
    """A permission the persona holds at every node the role is assigned at (anywhere for a role assigned nowhere):
    adding one the caller lacks there is refused past the gate (R36)"""
    auth = await load_auth_context(db, user)
    nodes = await role_nodes(db, role_id)
    names = [name for name in perms if all(auth.covers(name, node) for node in nodes)]
    return (await db.exec(select(Permission.id).where(Permission.name.in_(names)).order_by(Permission.id))).first() or 1


@pytest.mark.asyncio
@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("entry", ENTRIES, ids=[f"{e.method} {e.path}" for e in ENTRIES])
async def test_route_gate(client, db, headers, entry, persona):
    perms = persona_permissions(entry, persona)
    post = await make_post(db)
    user = await make_user(db, perms=perms, post_id=post.id)
    ids = await make_dictionary(db)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    media = await make_media(db, observation, user)
    role_id = (await db.exec(select(RoleAssignment.role_id).where(RoleAssignment.user_id == user.id)
                             .order_by(RoleAssignment.id))).first() or 1
    assignment_id = (await db.exec(select(RoleAssignment.id).where(RoleAssignment.user_id == user.id)
                                   .order_by(RoleAssignment.id))).first() or 1
    ids.update({
        "user_id": user.id,
        "post_id": post.id,
        "observation_id": observation.id,
        "media_id": media.id,
        "frequency_id": (await db.exec(select(ObservationFrequency.id).where(
            ObservationFrequency.observation_id == observation.id))).first(),
        "role_id": role_id,
        "assignment_id": assignment_id,
        # a permission the persona holds (where the role is assigned): granting or revoking one the caller lacks
        # is refused past the gate
        "permission_id": await grantable_permission(db, user, role_id, perms),
        "format_id": 1,
        "group_id": 1,
        "upload_id": "0" * 32,
        "session_id": "0" * 32,
    })

    path = entry.path.format(**ids)
    kwargs = {"headers": headers(user)}
    if entry.method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    result = await expected(db, entry, user)   # before the call: it may change the persona's own role
    response = await client.request(entry.method, path, **kwargs)

    if result.allowed:
        assert response.status_code not in (401, 403), f"{persona} should pass the gate: {response.text}"
    else:
        assert response.status_code == 403, f"{persona} should be rejected: {response.status_code} {response.text}"
        if entry.method == "HEAD":
            return  # HEAD answers carry no body (HTTP semantics); the status code is the whole contract
        detail = response.json()
        assert detail["code"] == "permission_denied"
        assert detail["missing"] == list(result.missing)
        assert detail["required"] == entry.require.as_dict()
