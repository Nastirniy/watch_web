"""The API contract of the web client is the OpenAPI document FastAPI builds: api-contract/openapi.json.

    python -m app.backend.api_contract [path]      write app.openapi() (default: api-contract/openapi.json)

The file is committed. tests/test_api_contract.py fails while the code and the file differ, so every change
of the API (paths, bodies, problem codes and their params) is a reviewable diff of this file. An intended
change is accepted by writing the file again (and a CHANGELOG.md entry); the web project regenerates its
types from the file with openapi-typescript and its tsc shows what the change breaks there (WEB_TOOLING.md).
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "api-contract" / "openapi.json"


def write(path: Path = OPENAPI_PATH) -> Path:
    from app.main import fastapi_app
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fastapi_app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(f"wrote {write(Path(sys.argv[1]) if len(sys.argv) > 1 else OPENAPI_PATH)}")
