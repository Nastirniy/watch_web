"""Role soft delete: hidden, not assignable, permissions stop applying, restorable."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.backend.database import seed_roles_and_permissions
from app.backend.enums.routes import RoleRoutes, TagRoutes, UserRoutes
from app.backend.models.role import Role, UserRole, Permission, RolePermission
from tests.helpers import make_post, make_user, make_admin, make_role

ROLES = RoleRoutes.BASE.versioned_path() + "/"
ROLE = RoleRoutes.ROLE_BY_ID.versioned_path()
TAGS = TagRoutes.LIST_ALL.versioned_path()


@pytest.mark.asyncio
async def test_soft_delete_lifecycle(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view"], name="viewer")
    user = await make_user(db, role=role, post_id=post.id)

    assert (await client.get(TAGS, headers=headers(user))).status_code == 200

    deleted = await client.delete(ROLE.format(role_id=role.id), headers=headers(admin))
    assert deleted.status_code == 204, deleted.text

    # permissions stop applying immediately
    denied = await client.get(TAGS, headers=headers(user))
    assert denied.status_code == 403
    assert denied.json()["detail"]["missing"] == ["tag.view"]

    # hidden from lists unless include_deleted
    listed = (await client.get(ROLES, headers=headers(admin))).json()
    assert role.id not in {r["id"] for r in listed}
    with_deleted = (await client.get(ROLES, params={"include_deleted": "true"}, headers=headers(admin))).json()
    assert {r["id"]: r["is_deleted"] for r in with_deleted}[role.id] is True

    # still inspectable by id for the administration GUI
    detail = await client.get(ROLE.format(role_id=role.id), headers=headers(admin))
    assert detail.status_code == 200 and detail.json()["is_deleted"] is True

    # cannot be deleted twice, cannot be assigned, name cannot be reused
    assert (await client.delete(ROLE.format(role_id=role.id), headers=headers(admin))).status_code == 404
    assign = await client.patch(UserRoutes.USER_BY_ID.versioned_path().format(user_id=user.id),
                                data={"role_ids": f"[{role.id}]"}, headers=headers(admin))
    assert assign.status_code == 400, assign.text
    reuse = await client.post(ROLES, json={"name": "viewer", "display_name": "Viewer"}, headers=headers(admin))
    assert reuse.status_code == 409

    # the user's role list hides it, /auth/me shows no roles
    user_roles = (await client.get(UserRoutes.USER_ROLES.versioned_path().format(user_id=user.id), headers=headers(admin))).json()
    assert user_roles == []
    me = (await client.get("/api/v1/auth/me", headers=headers(user))).json()
    assert me["roles"] == [] and me["permissions"] == []

    # restore
    restored = await client.patch(ROLE.format(role_id=role.id), json={"is_deleted": False}, headers=headers(admin))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False
    assert (await client.get(TAGS, headers=headers(user))).status_code == 200
    assert role.id in {r["id"] for r in (await client.get(ROLES, headers=headers(admin))).json()}


@pytest.mark.asyncio
async def test_seed_restores_deleted_admin_role(db):
    admin_role = (await db.exec(select(Role).where(Role.name == "admin"))).one()
    admin_role.is_deleted = True
    db.add(admin_role)
    await db.commit()
    await seed_roles_and_permissions(db)
    await db.refresh(admin_role)
    assert admin_role.is_deleted is False


@pytest.mark.asyncio
async def test_assignments_are_unique(db):
    role = await make_role(db, [])
    user = await make_user(db, role=role)
    role_id, user_id = role.id, user.id
    db.add(UserRole(user_id=user_id, role_id=role_id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    permission_id = (await db.exec(select(Permission.id).where(Permission.name == "tag.view"))).one()
    db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    await db.commit()
    db.add(RolePermission(role_id=role_id, permission_id=permission_id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
