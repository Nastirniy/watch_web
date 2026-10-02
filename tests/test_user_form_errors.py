"""POST /users/ and PATCH /users/{id}: request body and certificate errors answer 4xx, never a silent 200 or a 500."""
import pytest
from sqlmodel import select

from app.backend.models.role import Role
from app.backend.models.user import User
from tests.helpers import make_admin, make_user
from tests.test_username_validation import CREATE, USER, make_certificate

MAX_CERT_SIZE = 5 * 1024 * 1024


async def admin_role_id(db) -> int:
    return (await db.exec(select(Role).where(Role.name == "admin"))).one().id


@pytest.mark.asyncio
async def test_patch_with_json_body_answers_415(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db)
    response = await client.patch(USER.format(user_id=target.id), headers=headers(admin), json={"username": "new_name"})
    assert response.status_code == 415
    assert "multipart/form-data" in response.headers["accept-patch"]

    await db.refresh(target)
    assert target.username != "new_name"


@pytest.mark.asyncio
@pytest.mark.parametrize("as_multipart", [False, True])
async def test_patch_with_form_body_still_works(client, db, headers, as_multipart):
    admin = await make_admin(db)
    target = await make_user(db)
    files = {"certificate": ("cert.pem", make_certificate(f"cn-form-{target.id}"), "application/x-pem-file")} if as_multipart else None
    response = await client.patch(
        USER.format(user_id=target.id), headers=headers(admin), data={"username_alias": "New alias"}, files=files)
    assert response.status_code == 200, response.text
    assert response.json()["username_alias"] == "New alias"


@pytest.mark.asyncio
@pytest.mark.parametrize("content, code", [(b"", 400), (b"x" * (MAX_CERT_SIZE + 1), 413)], ids=["empty", "too_large"])
async def test_create_certificate_file_errors(client, db, headers, content, code):
    admin = await make_admin(db)
    response = await client.post(
        CREATE, headers=headers(admin),
        files={"certificate": ("cert.pem", content, "application/x-pem-file")},
        data={"username": "cert_case", "username_alias": "Alias", "role_ids": [await admin_role_id(db)]},
    )
    assert response.status_code == code, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("content, code", [(b"", 400), (b"x" * (MAX_CERT_SIZE + 1), 413)], ids=["empty", "too_large"])
async def test_update_certificate_file_errors(client, db, headers, content, code):
    admin = await make_admin(db)
    target = await make_user(db)
    response = await client.patch(
        USER.format(user_id=target.id), headers=headers(admin),
        files={"certificate": ("cert.pem", content, "application/x-pem-file")},
    )
    assert response.status_code == code, response.text


@pytest.mark.asyncio
async def test_update_with_certificate_of_another_user_answers_400(client, db, headers):
    admin = await make_admin(db)
    role_id = await admin_role_id(db)
    certificate = make_certificate("cn-shared")
    created = await client.post(
        CREATE, headers=headers(admin),
        files={"certificate": ("cert.pem", certificate, "application/x-pem-file")},
        data={"username": "cert_owner", "username_alias": "Alias", "role_ids": [role_id]},
    )
    assert created.status_code == 200, created.text

    other = await make_user(db)
    response = await client.patch(
        USER.format(user_id=other.id), headers=headers(admin),
        files={"certificate": ("cert.pem", certificate, "application/x-pem-file")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "certificate_taken"
