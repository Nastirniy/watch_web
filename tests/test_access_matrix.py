"""Table-driven access matrix over every guarded route.

For each route in the generated route access map and each persona, the call
must either pass the permission gate (status not 401/403) or be rejected with
a 403 carrying the expected code and missing list. Data scope is satisfied by
building every referenced resource inside the persona's own post.
"""
import pytest

from app.main import fastapi_app
from app.backend.auth.route_access import route_access_map
from app.backend.auth.context import AuthContext
from app.backend.models.observation import ObservationFrequency
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media, make_role

ENTRIES = list(route_access_map(fastapi_app))
PERSONAS = ["no_perms", "granular", "granular_admin", "admin_only"]


def persona_permissions(entry, persona) -> list:
    granular = list(entry.require.all) + list(entry.require.any[:1])
    return {
        "no_perms": [],
        "granular": granular,
        "granular_admin": granular + ["admin.true"],
        "admin_only": ["admin.true"],
    }[persona]


def expected(entry, perms):
    ctx = AuthContext(user=None, permissions=frozenset(perms), roles=(),
                      readable_post_ids=frozenset(), writable_post_ids=frozenset())
    return entry.require.evaluate(ctx)


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
    role_id = (await db.exec(__import__("sqlmodel").select(__import__("app.backend.models.role", fromlist=["UserRole"]).UserRole.role_id).where(
        __import__("app.backend.models.role", fromlist=["UserRole"]).UserRole.user_id == user.id))).first()
    ids.update({
        "user_id": user.id,
        "post_id": post.id,
        "observation_id": observation.id,
        "media_id": media.id,
        "frequency_id": (await db.exec(__import__("sqlmodel").select(ObservationFrequency.id).where(
            ObservationFrequency.observation_id == observation.id))).first(),
        "role_id": role_id,
        "permission_id": 1,
        "format_id": 1,
        "upload_id": "0" * 32,
        "session_id": "0" * 32,
    })

    path = entry.path.format(**ids)
    kwargs = {"headers": headers(user)}
    if entry.method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    response = await client.request(entry.method, path, **kwargs)

    result = expected(entry, perms)
    if result.allowed:
        assert response.status_code not in (401, 403), f"{persona} should pass the gate: {response.text}"
    else:
        assert response.status_code == 403, f"{persona} should be rejected: {response.status_code} {response.text}"
        if entry.method == "HEAD":
            return  # HEAD answers carry no body (HTTP semantics); the status code is the whole contract
        detail = response.json()
        assert detail["code"] == ("admin_required" if result.admin_missing else "permission_denied")
        assert detail["missing"] == list(result.missing)
        assert detail["required"] == entry.require.as_dict()
