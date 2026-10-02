"""The error contract of the web client (codes, params, codes per operation) changes only on purpose.

Fails when the code no longer matches api-contract/problems.lock.json; the message lists what changed and
which changes break the web client. Accept an intended change with: python -m app.backend.api_contract lock
"""
import copy

import pytest

from app.backend.api_contract import build_lock, diff_lock, read_lock, report
from app.backend.enums.routes import MediaRoutes
from tests.helpers import make_admin


@pytest.fixture(scope="module")
def current():
    from app.main import fastapi_app
    return build_lock(fastapi_app)


def test_code_matches_the_locked_contract(current):
    text = report(diff_lock(read_lock(), current))
    assert not text, "\n" + text


def test_renames_and_removals_are_breaking(current):
    locked = copy.deepcopy(current)
    changed = copy.deepcopy(current)
    changed["problems"]["post_not_found_v2"] = changed["problems"].pop("post_not_found")
    params = changed["problems"]["tag_name_taken"]["params"]
    params["tag_name"] = params.pop("name")
    changed["problems"]["upload_offset_mismatch"]["params"]["offset"]["type"] = "str"
    operation = changed["operations"]["update_post"]["problems"]
    operation["404"] = [c for c in operation["404"] if c != "post_not_found"]

    changes = diff_lock(locked, changed)
    breaking = "\n".join(changes["breaking"])
    assert "problem code removed or renamed: post_not_found" in breaking
    assert "tag_name_taken: param removed or renamed: name" in breaking
    assert "upload_offset_mismatch.offset: type int -> str" in breaking
    assert "update_post: no longer answers 404 post_not_found" in breaking
    additive = "\n".join(changes["additive"])
    assert "problem code added: post_not_found_v2" in additive
    assert "update_post" not in additive


def test_additions_are_reported_too(current):
    changed = copy.deepcopy(current)
    changed["operations"]["update_post"]["problems"].setdefault("409", []).append("tag_not_archived")
    changes = diff_lock(current, changed)
    assert not changes["breaking"]
    assert changes["additive"] == ["update_post: now answers 409 tag_not_archived (the web client must handle it)"]


def test_the_lock_names_codes_never_python_functions(current):
    """Moving or renaming Python code that keeps the same codes must not touch the lock"""
    text = str(current)
    for internal in ("app.backend", ".py", "<locals>", "AccessControlService"):
        assert internal not in text


@pytest.mark.asyncio
async def test_storage_statistics_do_not_expose_the_storage_path(client, db, headers):
    admin = await make_admin(db)
    response = await client.get(MediaRoutes.STORAGE_STATISTICS.versioned_path(), headers=headers(admin))
    assert response.status_code == 200, response.text
    assert "storage_path" not in response.json()
