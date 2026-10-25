"""Replacing the permissions of a role keeps the rows that stay, by difference. A user's roles are role assignments,
one link row each (the assignment API, no replacement)."""
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1.users import taken_error_code
from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import RoleRoutes, UserRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role import Permission, RolePermission
from tests.helpers import make_admin, make_role

ROLES = V1.path(RoleRoutes.ROOT)
ROLE = V1.path(RoleRoutes.ROLE_BY_ID)
USER = V1.path(UserRoutes.USER_BY_ID)


async def permission_ids(db, names):
    rows = await db.exec(select(Permission).where(Permission.name.in_(names)).order_by(Permission.name))
    return [p.id for p in rows.all()]


async def stored_permissions(db, role_id):
    rows = await db.exec(select(RolePermission.permission_id).where(RolePermission.role_id == role_id))
    return sorted(rows.all())


@pytest.mark.asyncio
@pytest.mark.parametrize("current, requested", [
    (["tag.view"], ["tag.view", "post.view"]),
    (["tag.view", "post.view"], ["tag.view"]),
    (["tag.view", "post.view"], ["post.view", "tag.view"]),
    (["tag.view", "post.view"], ["post.update", "post.archive"]),
])
async def test_role_permissions_replaced_by_difference(client, db, headers, current, requested):
    admin = await make_admin(db)
    role = await make_role(db, current)
    wanted = await permission_ids(db, requested)

    response = await client.patch(ROLE.format(role_id=role.id), headers=headers(admin), json={"permission_ids": wanted})

    assert response.status_code == 200, response.text
    assert await stored_permissions(db, role.id) == sorted(wanted)
    audit = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "role", AuditLog.action == "update"))).all()
    assert audit, "the change is still audited"


@pytest.mark.asyncio
async def test_role_create_stores_the_requested_permissions(client, db, headers):
    admin = await make_admin(db)
    wanted = await permission_ids(db, ["tag.view", "tag.create"])
    response = await client.post(ROLES, headers=headers(admin), json={
        "name": "linked", "display_name": "Linked", "permission_ids": wanted})
    assert response.status_code == 201, response.text
    assert await stored_permissions(db, response.json()["id"]) == sorted(wanted)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "patch"])
async def test_repeated_permission_ids_answer_the_field_error(client, db, headers, method):
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view"])
    pid = (await permission_ids(db, ["tag.view"]))[0]
    if method == "post":
        response = await client.post(ROLES, headers=headers(admin), json={
            "name": "dup", "display_name": "Dup", "permission_ids": [pid, pid]})
    else:
        response = await client.patch(ROLE.format(role_id=role.id), headers=headers(admin), json={"permission_ids": [pid, pid]})
    assert response.status_code == 422, response.text
    error = response.json()["errors"][0]
    assert error["code"] == "permission_ids_duplicate"
    assert error["pointer"] == "#/permission_ids"


class MySQLError(Exception):
    """Shape of pymysql.err.*: args = (errno, message)"""


def integrity(orig):
    return IntegrityError("UPDATE users SET username=%s", ("x",), orig)


def test_unique_violation_mapping():
    sqlite_error = sqlite3.IntegrityError
    assert taken_error_code(integrity(sqlite_error("UNIQUE constraint failed: users.username"))) == ErrorCode.USERNAME_TAKEN
    assert taken_error_code(integrity(sqlite_error(
        "UNIQUE constraint failed: user_certificates.fingerprint_hash"))) == ErrorCode.CERTIFICATE_TAKEN
    assert taken_error_code(integrity(MySQLError(1062, "Duplicate entry 'a' for key 'users.ix_users_username'"))) == ErrorCode.USERNAME_TAKEN
    assert taken_error_code(integrity(MySQLError(
        1062, "Duplicate entry 'a' for key 'user_certificates.uq_user_certificates_fingerprint_hash'",
    ))) == ErrorCode.CERTIFICATE_TAKEN
    # the unique index of role_assignments: a type change met a concurrent assignment of the new base role
    assert taken_error_code(integrity(sqlite_error(
        "UNIQUE constraint failed: role_assignments.user_id, role_assignments.role_id, role_assignments.scope_key",
    ))) == ErrorCode.ASSIGNMENT_EXISTS
    assert taken_error_code(integrity(MySQLError(
        1062, "Duplicate entry '1-2-root' for key 'role_assignments.uq_role_assignments_user_role_scope'",
    ))) == ErrorCode.ASSIGNMENT_EXISTS
    assert taken_error_code(integrity(MySQLError(
        1062, "Duplicate entry '1-2-root' for key 'uq_role_assignments_user_role_scope'"))) == ErrorCode.ASSIGNMENT_EXISTS
    # a violation of another unique constraint is none of these
    assert taken_error_code(integrity(sqlite_error("UNIQUE constraint failed: scope_groups.parent_key, scope_groups.name"))) is None
