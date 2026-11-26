"""Authorization v3: the catalog check at start, the report of grants that put less in effect than their roles hold,
and the authorization part of /health (counts only)."""
import pytest
from loguru import logger
from sqlmodel import select

from app.backend.auth.integrity import authorization_report
from app.backend.auth.scope import Node
from app.backend.bootstrap.authorization import check_authorization_catalog, log_authorization_report
from app.backend.config import settings
from app.backend.enums.general import BuiltinRole, RoleKind, UserType
from app.backend.enums.routes import SystemRoutes
from app.backend.models.role import Permission, Role, RolePermission
from tests.helpers import assign, builtin_role, make_admin, make_post, make_role, make_user


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)


async def _violations(db):
    post = await make_post(db)
    viewer = await make_user(db, post_id=post.id, user_type=UserType.VIEWER)
    broken = await make_role(db, ["post.view", "database.view"], kind=RoleKind.TREE, allow_mixed=True)
    below_root = await assign(db, viewer, broken, Node.post(post.id))
    beyond = await assign(db, viewer, await make_role(db, ["user.create"]), Node.post(post.id))
    return viewer, below_root, beyond


async def test_report_lists_what_grants_leave_out(db):
    await make_admin(db)
    viewer, below_root, beyond = await _violations(db)
    report = await authorization_report(db)
    assert report.system_administrators == 1
    found = {(v.kind, v.assignment_id): v for v in report.violations}
    assert found[("tree", below_root.id)].left_out == {"database.view"}
    assert found[("tree", beyond.id)].left_out == {"user.create"}
    assert found[("tree", beyond.id)].user_id == viewer.id and found[("tree", beyond.id)].scope_key == f"p:{viewer.post_id}"


async def test_clean_database_has_no_violations(db):
    await make_admin(db)
    report = await authorization_report(db)
    assert report.violations == () and report.system_administrators == 1


async def test_archived_or_inactive_administrators_do_not_count(db):
    admin = await make_admin(db)
    admin.is_active = False
    db.add(admin)
    await db.commit()
    assert (await authorization_report(db)).system_administrators == 0


async def test_catalog_check_refuses_a_level_changed_without_migration(db):
    await check_authorization_catalog(db)
    row = (await db.exec(select(Permission).where(Permission.name == "audit.view"))).one()
    row.level = "tree"
    db.add(row)
    await db.commit()
    with pytest.raises(RuntimeError, match="audit.view"):
        await check_authorization_catalog(db)


async def test_catalog_check_refuses_a_builtin_role_out_of_sync(db):
    role = await builtin_role(db, BuiltinRole.AUDITOR)
    await db.execute(RolePermission.__table__.delete().where(RolePermission.role_id == role.id))
    await db.commit()
    with pytest.raises(RuntimeError, match="auditor"):
        await check_authorization_catalog(db)


async def test_log_names_ids_only_and_warns_without_an_administrator(db):
    viewer, below_root, _ = await _violations(db)
    lines = []
    sink = logger.add(lambda message: lines.append((message.record["level"].name, message.record["message"])),
                      level="WARNING")
    try:
        await log_authorization_report(db)
    finally:
        logger.remove(sink)
    warnings = [text for level, text in lines if level == "WARNING"]
    assert any(f"tree:{below_root.id}" in text and "database.view" in text for text in warnings)
    assert all(viewer.username not in text for _, text in lines)
    assert any(level == "ERROR" and "init_user" in text and "system_administrator" in text for level, text in lines)


async def test_health_counts_only(client, db):
    await make_admin(db)
    viewer, _, _ = await _violations(db)
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["authorization"] == {"status": "degraded", "violations": 2, "system_administrators": 1}
    assert body["status"] == "degraded"
    assert viewer.username not in str(body["authorization"])


async def test_health_healthy_authorization(client, db):
    await make_admin(db)
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["authorization"] == {"status": "healthy", "violations": 0, "system_administrators": 1}
