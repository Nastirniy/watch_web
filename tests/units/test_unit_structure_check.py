"""Start-up check: corps > brigade > post is the only unit structure (bootstrap/structure.py)."""
import pytest

from app.backend.bootstrap.structure import check_unit_structure, misplaced_units
from tests.helpers import make_group


async def test_no_rows_and_a_correct_tree_are_fine(db):
    assert await misplaced_units(db) == []
    corps = await make_group(db, kind="corps")
    await make_group(db, parent_id=corps.id, kind="brigade")
    plain = await make_group(db)
    await make_group(db, parent_id=plain.id)
    assert await misplaced_units(db) == []
    await check_unit_structure(db)


async def test_brigade_under_the_root_is_listed(db):
    brigade = await make_group(db, kind="brigade")
    found = await misplaced_units(db)
    assert [(m.group_id, m.kind, m.parent_id, m.parent_kind) for m in found] == [(brigade.id, "brigade", None, None)]


async def test_brigade_under_a_plain_group_is_listed(db):
    plain = await make_group(db)
    brigade = await make_group(db, parent_id=plain.id, kind="brigade")
    found = await misplaced_units(db)
    assert [(m.group_id, m.parent_id, m.parent_kind) for m in found] == [(brigade.id, plain.id, "group")]


async def test_corps_under_a_group_is_listed(db):
    plain = await make_group(db)
    corps = await make_group(db, parent_id=plain.id, kind="corps")
    found = await misplaced_units(db)
    assert [(m.group_id, m.kind, m.parent_kind) for m in found] == [(corps.id, "corps", "group")]


async def test_archived_misplaced_brigade_is_listed(db):
    brigade = await make_group(db, kind="brigade")
    brigade.is_deleted = True
    await db.commit()
    assert [m.group_id for m in await misplaced_units(db)] == [brigade.id]


async def test_check_raises_naming_the_row_and_the_fix(db):
    brigade = await make_group(db, kind="brigade")
    with pytest.raises(RuntimeError) as caught:
        await check_unit_structure(db)
    message = str(caught.value)
    assert str(brigade.id) in message and "brigade" in message
    assert "no API route creates" in message
    assert "UPDATE scope_groups SET parent_id" in message and "DEPLOYMENT.md" in message


async def test_startup_stops_on_a_misplaced_unit(db, engine, session_maker, monkeypatch):
    """The start runs the check after seeding: a brigade without a corps stops it"""
    from app.backend.bootstrap import startup

    async def _session_maker():
        return session_maker

    async def _engine():
        return engine

    async def _nothing():
        return None

    monkeypatch.setattr(startup, "run_alembic_upgrade", lambda: None)
    monkeypatch.setattr(startup, "seed_database", _nothing)
    monkeypatch.setattr(startup, "create_database_if_not_exists", _nothing)
    monkeypatch.setattr(startup, "get_session_maker", _session_maker)
    monkeypatch.setattr(startup, "get_engine", _engine)
    await startup.initialize_database_with_migrations()    # a correct (empty) structure starts
    await make_group(db, kind="brigade")
    with pytest.raises(RuntimeError):
        await startup.initialize_database_with_migrations()
