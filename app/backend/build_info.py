"""Which release this code is: the tag and the commit the image was built from (RELEASE.md).

The Dockerfile writes `app/build_info.json` from the build args `deploy/release.sh build` passes:
`{"version": "<YYYY.MM.DD>-<sha7>", "revision": "<full sha>", "created": "<commit time, ISO 8601 UTC>"}`.
Without the file (a local run, the tests) the version is `dev`. A malformed file raises, so a broken image never
starts and never reports a version it is not.

The version shows in `/health`, the worker heartbeat and `cli/schema_check status`; never in a response header and
never in the OpenAPI document, whose `info.version` is the contract version (`API_DOCUMENT_VERSION`, app/main.py).
"""
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

BUILD_INFO_PATH = Path(__file__).resolve().parents[1] / "build_info.json"
DEV_VERSION = "dev"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    revision: Optional[str]
    created: Optional[str]


def _optional_text(data: dict, key: str) -> Optional[str]:
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{BUILD_INFO_PATH}: {key} must be a string")
    return value


@lru_cache(maxsize=1)
def build_info() -> BuildInfo:
    try:
        text = BUILD_INFO_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return BuildInfo(DEV_VERSION, None, None)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{BUILD_INFO_PATH} is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{BUILD_INFO_PATH} must hold an object")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{BUILD_INFO_PATH}: version must be a non-empty string")
    return BuildInfo(version, _optional_text(data, "revision"), _optional_text(data, "created"))
