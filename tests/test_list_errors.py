"""List routes let their problems through: no catch-all turns a 403 into a 500."""
import pytest

from app.backend.api.v1 import V1
from app.backend.enums.routes import RoleRoutes, UserRoutes
from tests.helpers import make_user


@pytest.mark.asyncio
@pytest.mark.parametrize("url, perms", [
    (V1.path(RoleRoutes.ROOT), ["role.view", "admin.true"]),
    (V1.path(UserRoutes.ROOT), ["user.view"]),
])
async def test_include_deleted_without_archive_permission_is_403(client, db, headers, url, perms):
    user = await make_user(db, perms=perms)
    response = await client.get(url, params={"include_deleted": "true"}, headers=headers(user))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "permission_required_for_action"
