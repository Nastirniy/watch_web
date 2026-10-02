"""Username rules on POST /users/ and PATCH /users/{id}: structured 422, case kept, case-insensitive uniqueness."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlmodel import select

from app.backend.enums.routes import UserRoutes
from app.backend.models.role import Role
from tests.helpers import make_admin, make_user

CREATE = UserRoutes.ROOT.versioned_path()
USER = UserRoutes.USER_BY_ID.versioned_path()


def make_certificate(common_name: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


async def create_user(client, headers, role_id: int, username: str):
    return await client.post(
        CREATE, headers=headers,
        files={"certificate": ("cert.pem", make_certificate(f"cn-{username}"), "application/x-pem-file")},
        data={"username": username, "username_alias": "Alias", "role_ids": f"[{role_id}]"},
    )


async def admin_role_id(db) -> int:
    return (await db.exec(select(Role).where(Role.name == "admin"))).one().id


def username_error(response) -> dict:
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"
    errors = [e for e in response.json()["errors"] if e["pointer"] == "#/username"]
    assert len(errors) == 1, response.json()
    return errors[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["a", "   a   "])
async def test_too_short_username_answers_the_field_error(client, db, headers, username):
    """One letter fails the form check, one letter padded with spaces fails after stripping; both say why."""
    admin = await make_admin(db)
    error = username_error(await create_user(client, headers(admin), await admin_role_id(db), username))
    assert error == {"code": "string_too_short", "detail": "String should have at least 3 characters",
                     "pointer": "#/username", "min_length": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize("username, code", [
    ("ADMIN", "username_reserved"),
    ("ivan petrenko", "username_invalid"),
])
async def test_rule_violations_are_not_hidden(client, db, headers, username, code):
    admin = await make_admin(db)
    role_id = await admin_role_id(db)
    create_error = username_error(await create_user(client, headers(admin), role_id, username))
    assert create_error["code"] == code
    assert not create_error["detail"].startswith("Value error")

    target = await make_user(db)
    update_error = username_error(await client.patch(
        USER.format(user_id=target.id), headers=headers(admin), data={"username": username}))
    assert update_error["code"] == code


@pytest.mark.asyncio
async def test_invalid_role_ids_answer_the_field_error(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db)
    response = await client.patch(USER.format(user_id=target.id), headers=headers(admin), data={"role_ids": "[1, 1]"})
    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["pointer"] == "#/role_ids"
    assert error["code"] == "role_ids_duplicate"


@pytest.mark.asyncio
async def test_username_keeps_its_case(client, db, headers):
    admin = await make_admin(db)
    created = await create_user(client, headers(admin), await admin_role_id(db), "Ivan.Petrenko")
    assert created.status_code == 200, created.text
    assert created.json()["username"] == "Ivan.Petrenko"

    target = await make_user(db)
    updated = await client.patch(USER.format(user_id=target.id), headers=headers(admin), data={"username": "Olena_K"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["username"] == "Olena_K"


@pytest.mark.asyncio
async def test_username_uniqueness_ignores_case(client, db, headers):
    admin = await make_admin(db)
    await make_user(db, username="Taken.Name")
    other = await make_user(db)

    created = await create_user(client, headers(admin), await admin_role_id(db), "taken.name")
    assert created.status_code == 400
    assert created.json()["code"] == "username_taken"

    updated = await client.patch(USER.format(user_id=other.id), headers=headers(admin), data={"username": "TAKEN.NAME"})
    assert updated.status_code == 400
    assert updated.json()["code"] == "username_taken"


@pytest.mark.asyncio
async def test_username_race_answers_400_not_certificate_error(client, db, headers, monkeypatch):
    """Another request took the name between the check and the commit: the unique index answers, reported as 400"""
    from app.backend.api.v1 import users as users_routes
    from app.backend.models.user import User
    monkeypatch.setattr(users_routes, "_username_taken", lambda *args, **kwargs: select(User).where(User.id == -1))

    admin = await make_admin(db)
    await make_user(db, username="race_name")
    other = await make_user(db)
    response = await client.patch(USER.format(user_id=other.id), headers=headers(admin), data={"username": "race_name"})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "username_taken"


@pytest.mark.asyncio
async def test_user_can_change_only_the_case_of_own_username(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db, username="petro")
    response = await client.patch(USER.format(user_id=target.id), headers=headers(admin), data={"username": "Petro"})
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "Petro"
