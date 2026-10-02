"""Search returns the response model of the entity's list route, never raw table rows."""
import pytest

from app.backend.api.shared.models.response.user_response import UserResponse
from app.backend.api.v1 import V1
from app.backend.enums.routes import SearchRoutes, TagRoutes
from tests.helpers import make_admin, make_user


@pytest.mark.asyncio
async def test_user_search_never_returns_certificate_hashes(client, db, headers):
    admin = await make_admin(db)
    await make_user(db)
    body = (await client.get(V1.path(SearchRoutes.USERS), headers=headers(admin))).json()
    assert body["entity"] == "users" and body["items"], body
    for item in body["items"]:
        assert not any(key.startswith("pki_") for key in item), item
        assert set(item) <= set(UserResponse.model_fields), set(item) - set(UserResponse.model_fields)


@pytest.mark.asyncio
async def test_search_items_have_the_list_route_shape(client, db, headers):
    admin = await make_admin(db)
    await client.post(V1.path(TagRoutes.CREATE), json={"name": "search-shape"}, headers=headers(admin))
    listed = (await client.get(V1.path(TagRoutes.LIST_ALL), headers=headers(admin))).json()
    found = (await client.get(V1.path(SearchRoutes.TAGS), headers=headers(admin))).json()["items"]
    assert found and listed
    assert set(found[0]) == set(listed[0])
