"""HTTP calls to the thumbnail renderer.

Used only when nginx is disabled (the API proxies imgproxy's response) and
for cache pre-warming. Behind nginx the API never fetches an image itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import httpx

from app.backend.utils.logging_config import get_api_logger

_logger = get_api_logger()

RENDER_TIMEOUT_SECONDS = 30.0


@dataclass
class Rendered:
    status_code: int
    headers: Dict[str, str]
    body: bytes


async def fetch(url: str) -> Rendered:
    """GET a rendered thumbnail. Raises httpx.HTTPError when the renderer is unreachable."""
    async with httpx.AsyncClient(timeout=RENDER_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
    return Rendered(response.status_code, dict(response.headers), response.content)


async def prewarm(url: str) -> None:
    """Request a thumbnail once so nginx caches it before the first viewer asks. Failures are only logged."""
    try:
        async with httpx.AsyncClient(timeout=RENDER_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
        if response.status_code != 200:
            _logger.warning(f"thumbnail pre-warm returned {response.status_code} for {url}")
    except httpx.HTTPError as exc:
        _logger.warning(f"thumbnail pre-warm failed: {exc}")
