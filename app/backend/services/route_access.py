"""Route access map generated from route metadata.

Every route guarded with Depends(require(...)) has a Require among its dependencies.
The map is built on first use from the routes of the app (route_access_map) and
filtered per caller by the /auth/route-access endpoint. Routes without a require()
dependency (login, refresh, health) are not part of the map.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import Require


@dataclass(frozen=True)
class RouteAccessEntry:
    method: str
    path: str            # full template, e.g. /api/v1/users/{user_id}/post-access
    name: str            # handler function name
    tags: Tuple[str, ...]
    require: Require


def _find_require(dependant) -> Optional[Require]:
    for sub in dependant.dependencies:
        if isinstance(sub.call, Require):
            return sub.call
        nested = _find_require(sub)
        if nested is not None:
            return nested
    return None


def build_route_access_map(app: FastAPI) -> Tuple[RouteAccessEntry, ...]:
    entries: List[RouteAccessEntry] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        spec = _find_require(route.dependant)
        if spec is None:
            continue
        for method in sorted(route.methods or ()):
            entries.append(RouteAccessEntry(
                method=method,
                path=route.path,
                name=route.name,
                tags=tuple(str(tag) for tag in (route.tags or ())),
                require=spec,
            ))
    return tuple(entries)


def route_access_map(app: FastAPI) -> Tuple[RouteAccessEntry, ...]:
    """Built on first use from the routes the app has by then, cached in app.state"""
    entries = getattr(app.state, "route_access_map", None)
    if entries is None:
        entries = app.state.route_access_map = build_route_access_map(app)
    return entries


def routes_allowed_for(entries: Tuple[RouteAccessEntry, ...], auth: AuthContext) -> List[RouteAccessEntry]:
    return [entry for entry in entries if entry.require.evaluate(auth).allowed]
