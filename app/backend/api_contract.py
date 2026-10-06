"""The API contract of the web client is the OpenAPI document of each version: api-contract/<vN>/openapi.json.

    python -m app.backend.api_contract [directory]      write every version's document (default: api-contract/)

The files are committed. tests/test_api_contract.py fails while the code and a file differ, so every change of
the API (paths, bodies, problem codes and their params) is a reviewable diff of these files. An intended change
is accepted by writing the files again (and a CHANGELOG.md entry); the web project regenerates its types from
them with openapi-typescript and its tsc shows what the change breaks there (WEB_TOOLING.md).
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO_ROOT / "api-contract"


def contract_path(version, directory: Path = CONTRACT_DIR) -> Path:
    """api-contract/v1/openapi.json for the PublishedVersion of APIVersionRoutes.V1"""
    return directory / version.version.name.lower() / "openapi.json"


def write(directory: Path = CONTRACT_DIR) -> list:
    from app.main import fastapi_app
    from app.backend.api.versioning import PublishedVersion
    written = []
    for version in PublishedVersion.published.values():
        path = contract_path(version, directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(version.openapi(fastapi_app), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for written in write(Path(sys.argv[1]) if len(sys.argv) > 1 else CONTRACT_DIR):
        print(f"wrote {written}")
