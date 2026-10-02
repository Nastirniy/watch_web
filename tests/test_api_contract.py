"""The API contract of the web client (api-contract/openapi.json) changes only on purpose.

Fails while app.openapi() and the committed file differ and names what differs. Accept an intended change
with: python -m app.backend.api_contract (then add a CHANGELOG.md entry; the web team regenerates its types).
"""
import json

import pytest

from app.backend.api_contract import OPENAPI_PATH
from app.backend.enums.routes import MediaRoutes
from app.backend.api.v1 import V1
from app.main import fastapi_app
from tests.helpers import make_admin


def _differences(committed: dict, current: dict) -> list:
    lines = []
    for path in sorted(set(committed["paths"]) | set(current["paths"])):
        old, new = committed["paths"].get(path, {}), current["paths"].get(path, {})
        for method in sorted(set(old) | set(new)):
            if method not in new:
                lines.append(f"removed {method.upper()} {path}")
            elif method not in old:
                lines.append(f"added {method.upper()} {path}")
            elif old[method] != new[method]:
                lines.append(f"changed {method.upper()} {path} ({new[method].get('operationId')})")
    old_schemas = committed.get("components", {}).get("schemas", {})
    new_schemas = current.get("components", {}).get("schemas", {})
    for name in sorted(set(old_schemas) | set(new_schemas)):
        if old_schemas.get(name) != new_schemas.get(name):
            state = "removed" if name not in new_schemas else "added" if name not in old_schemas else "changed"
            lines.append(f"{state} schema {name}")
    for key in sorted((set(committed) | set(current)) - {"paths", "components"}):
        if committed.get(key) != current.get(key):
            lines.append(f"changed {key}")
    return lines


def test_openapi_matches_the_committed_contract():
    committed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    differences = _differences(committed, fastapi_app.openapi())
    assert not differences, ("api-contract/openapi.json differs from the code:\n  " + "\n  ".join(differences)
                             + "\nIf intended: python -m app.backend.api_contract, commit the file, add a CHANGELOG entry.")


@pytest.mark.asyncio
async def test_storage_statistics_do_not_expose_the_storage_path(client, db, headers):
    admin = await make_admin(db)
    response = await client.get(V1.path(MediaRoutes.STORAGE_STATISTICS), headers=headers(admin))
    assert response.status_code == 200, response.text
    assert "storage_path" not in response.json()
