"""Ids in paths and bodies are validated by FastAPI / pydantic: Path(ge=1), PositiveInt."""
import pytest

from app.backend.api.v1 import V1
from app.backend.enums.routes import RoleRoutes, UserRoutes
from tests.helpers import make_admin


@pytest.mark.asyncio
@pytest.mark.parametrize("url, parameter", [
    (V1.path(UserRoutes.USER_BY_ID, user_id=0), "user_id"),
    (V1.path(RoleRoutes.ROLE_BY_ID, role_id=0), "role_id"),
    (V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=1, permission_id=-3), "permission_id"),
])
async def test_ids_below_one_are_validation_errors(client, db, headers, url, parameter):
    admin = await make_admin(db)
    method = client.put if parameter == "permission_id" else client.get
    body = (await method(url, headers=headers(admin))).json()
    assert body["code"] == "validation_error", body
    assert body["errors"][0]["parameter"] == parameter and body["errors"][0]["location"] == "path"
    assert body["errors"][0]["code"] == "greater_than_equal"


@pytest.mark.asyncio
async def test_permission_ids_in_a_body_must_be_positive(client, db, headers):
    admin = await make_admin(db)
    response = await client.post(V1.path(RoleRoutes.ROOT), json={"name": "r1", "display_name": "R1", "permission_ids": [0]},
                                 headers=headers(admin))
    body = response.json()
    assert response.status_code == 422 and body["errors"][0]["code"] == "greater_than", body
    assert body["errors"][0]["pointer"] == "#/permission_ids/0"
