"""Randomized model-based test of corps / brigades.

A shadow model (plain dicts) of groups, posts and observations is driven by seeded random operation sequences. Every
operation goes to the API as the admin; the status (and problem code) must match the model's prediction, and the
model is updated only on success. Invariants (a)-(g) are compared against the API every few operations. A second
test runs the same sequences with a non-admin viewer holding a role at one corps or brigade.

Failures are collected per seed (never stop at the first), distinct failure keys are replayed through a small
delta-debugging loop to get a short reproduction, and everything is summarised by test_zz_summary at the end. Each
seed test also fails on its own mismatches, so -k, -n (xdist) or a deselected summary never hide one.
Env: FUZZ_SEEDS (default 6; 24+ for a deep run), FUZZ_OPS (default 60), FUZZ_MINIMIZE (default 1), FUZZ_OUT (json report path).
"""
import io
import json
import os
import random
import secrets
import tempfile
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import openpyxl
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.backend.api.v1 import V1
from app.backend.auth import sessions as session_lifetimes
from app.backend.auth.scope import Node
from app.backend.auth.service import AuthService
from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.database import get_session
from app.backend.enums.general import Permissions
from app.backend.enums.routes import (BrigadeRoutes, CorpsRoutes, MediaRoutes, ObservationRoutes, PostRoutes,
                                      ScopeGroupRoutes, SearchRoutes, UserRoutes)
from app.backend.models.user import UserSession
from app.backend.services.media.formats import invalidate_registry_cache, seed_media_formats
from app.main import fastapi_app
from tests.conftest import _TestSession
from app.backend.models.observation import Observation
from tests.helpers import (assign, errors_are_problems, make_admin, make_dictionary, make_media, make_role, make_user,
                           observation_payload)

SEEDS = int(os.environ.get("FUZZ_SEEDS", "6"))
SEED_BASE = int(os.environ.get("FUZZ_SEED_BASE", "0"))
OPS = int(os.environ.get("FUZZ_OPS", "60"))
MINIMIZE = os.environ.get("FUZZ_MINIMIZE", "1") == "1"
MINIMIZE_BUDGET_S = float(os.environ.get("FUZZ_MINIMIZE_BUDGET", "150"))
OUT = os.environ.get("FUZZ_OUT", os.path.join(tempfile.gettempdir(), "units_fuzz_report.json"))

USER_HEADER = "Користувач"

CORPS = V1.path(CorpsRoutes.ROOT)
BRIGADES = V1.path(BrigadeRoutes.ROOT)
POSTS = V1.path(PostRoutes.ROOT)
GROUPS = V1.path(ScopeGroupRoutes.ROOT)
OBS_CREATE = V1.path(ObservationRoutes.CREATE)
OBS_LIST = V1.path(ObservationRoutes.LIST_ALL)
OBS_REPORT = V1.path(ObservationRoutes.REPORT)
OBS_SEARCH = V1.path(SearchRoutes.OBSERVATIONS)
MEDIA = V1.path(MediaRoutes.ROOT)
USERS = V1.path(UserRoutes.ROOT)
POSTS_SEARCH = V1.path(SearchRoutes.POSTS)

NAME_POOL = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
UNKNOWN = 99999

import collections
OUTCOMES = collections.Counter()
RESULTS = []        # one dict per seed run
STATS = {"seeds": 0, "ops": 0, "ops_skipped": 0, "light_checks": 0, "heavy_checks": 0, "requests": 0,
         "report_checks": 0, "viewer_checks": 0}


class Skip(Exception):
    """A scripted op refers to something a shortened replay never created"""


# ---------------------------------------------------------------------------------------------- environment
@asynccontextmanager
async def fresh_env():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    issued = []
    maker = async_sessionmaker(engine, class_=_TestSession, expire_on_commit=False, info={"issued_sessions": issued})

    def headers(user) -> dict:
        now = datetime.now(timezone.utc)
        row = UserSession(id=secrets.token_urlsafe(32), user_id=user.id, created_at=now, last_activity=now, expires_at=now)
        session_lifetimes.touch(row, now)
        issued.append(row)
        return {"Authorization": f"Bearer {AuthService.create_access_token(user.id, session_id=row.id)}"}

    async with maker() as db:
        await seed_roles_and_permissions(db)
        await seed_media_formats(db)
        invalidate_registry_cache()

        async def override():
            async with maker() as session:
                yield session

        fastapi_app.dependency_overrides[get_session] = override
        transport = ASGITransport(app=fastapi_app, raise_app_exceptions=False)
        try:
            async with AsyncClient(transport=transport, base_url="http://test",
                                   event_hooks={"response": [errors_are_problems]}) as client:
                admin = await make_admin(db)
                dictionary = await make_dictionary(db)
                yield SimpleNamespace(db=db, client=client, headers=headers, admin=admin, dictionary=dictionary)
        finally:
            fastapi_app.dependency_overrides.clear()
    await engine.dispose()
    invalidate_registry_cache()


# ---------------------------------------------------------------------------------------------- the model
class Model:
    def __init__(self):
        self.groups = {}    # id -> {kind, parent, archived, name}
        self.posts = {}     # id -> {group, archived}
        self.obs = {}       # id -> {post, corps, brigade, archived}
        self.users = {}     # id -> home post
        self.media = {}     # id -> observation id

    def chain(self, group_id):
        out = []
        while group_id is not None and group_id in self.groups and group_id not in out:
            out.append(group_id)
            group_id = self.groups[group_id]["parent"]
        return out

    def units_of_group(self, group_id):
        units = {}
        for gid in self.chain(group_id):
            kind = self.groups[gid]["kind"]
            if kind in ("corps", "brigade"):
                units[kind] = gid
        return units

    def units_of_post(self, post_id):
        return self.units_of_group(self.posts[post_id]["group"])

    def posts_under(self, node_id):
        return {pid for pid, p in self.posts.items() if node_id in self.chain(p["group"])}

    def name_taken(self, parent, name, exclude=None):
        return any(gid != exclude and g["parent"] == parent and g["name"].lower() == name.lower()
                   for gid, g in self.groups.items())

    def kids(self, group_id, active=True):
        return [gid for gid, g in self.groups.items() if g["parent"] == group_id and not (active and g["archived"])]

    def obs_expected(self, units, placement, include_deleted):
        """units: {kind: id}; all must hold"""
        found = set()
        for oid, o in self.obs.items():
            if o["archived"] and not include_deleted:
                continue
            ok = True
            for kind, uid in units.items():
                g = self.groups.get(uid)
                if g is None or g["kind"] != kind:
                    ok = False
                    break
                if placement == "recorded":
                    ok = o[kind] == uid
                else:
                    p = self.posts[o["post"]]
                    ok = (not p["archived"]) and uid in self.chain(p["group"])
                if not ok:
                    break
            if ok:
                found.add(oid)
        return found

    def height(self, group_id):
        """Levels of the live subtree under the group, the group itself included"""
        level, h = [group_id], 0
        while level:
            h += 1
            level = [k for g in level for k in self.kids(g)]
        return h

    def unit_ids(self):
        return [gid for gid, g in self.groups.items() if g["kind"] in ("corps", "brigade")]


# ---------------------------------------------------------------------------------------------- the simulation
class Sim:
    def __init__(self, env, seed, viewer=False):
        self.env = env
        self.client = env.client
        self.h = env.headers(env.admin)
        self.rng = random.Random(seed)
        self.seed = seed
        self.model = Model()
        self.sym = {}           # symbol -> real id
        self.sym_of = {}        # real id -> symbol
        self.counter = 0
        self.script = []        # [(op, args)] as generated
        self.log = []           # readable executed requests
        self.fails = {}         # key -> dict
        self.viewer = None      # (user, headers, node id)
        self.want_viewer = viewer
        self.op_no = 0

    # ---- plumbing
    def fail(self, key, message):
        entry = self.fails.get(key)
        if entry is None:
            self.fails[key] = {"key": key, "message": message, "op_no": self.op_no, "count": 1,
                               "script": list(self.script), "log": list(self.log)}
        else:
            entry["count"] += 1

    async def call(self, method, path, headers=None, **kw):
        STATS["requests"] += 1
        r = await self.client.request(method, path, headers=headers or self.h, **kw)
        return r

    def ref(self, x):
        if isinstance(x, str):
            if x not in self.sym:
                raise Skip()
            return self.sym[x]
        return x

    def sym_for(self, real):
        return self.sym_of.get(real, real)

    def bind(self, symbol, real):
        self.sym[symbol] = real
        self.sym_of[real] = symbol

    def new_symbol(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    async def step(self, name, method, path, expect, on_ok=None, unit=None, post=None, **kw):
        r = await self.call(method, path, **kw)
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        self.log.append(f"{method} {path} {json.dumps(kw.get('json')) if kw.get('json') else ''} "
                        f"{kw.get('params') or ''} -> {r.status_code}"
                        f"{' ' + str(body.get('code')) if r.status_code >= 400 and isinstance(body, dict) else ''}")
        OUTCOMES[f"{name}: {expect}"] += 1
        if isinstance(expect, tuple):
            want_status, want_code = expect
            got_code = body.get("code") if isinstance(body, dict) else None
            ok = r.status_code == want_status and got_code == want_code
        else:
            want_status, want_code = "2xx", None
            ok = 200 <= r.status_code < 300
        if not ok:
            got = (r.status_code, body.get("code") if r.status_code >= 400 and isinstance(body, dict) else None)
            self.fail(("op", name, str(expect), str(got)),
                      f"{name}: {method} {path} json={kw.get('json')} params={kw.get('params')}: expected "
                      f"{expect}, got {got} {(r.text[:200] if r.status_code >= 400 else '')}")
            await self.resync()
            return None
        if isinstance(expect, tuple):
            return None
        if on_ok is not None:
            on_ok(body)
        if unit is not None:
            self.check_unit_body(name, body, unit() if callable(unit) else unit)
        if post is not None:
            self.check_post_body(name, body, post() if callable(post) else post)
        return body

    def check_unit_body(self, name, body, gid):
        """The unit an answer returns matches the model after the change"""
        m = self.model
        g = m.groups[gid]
        want = {"id": gid, "name": g["name"], "is_deleted": g["archived"]}
        if g["kind"] == "brigade":
            want["corps_id"] = g["parent"]
        if g["kind"] == "corps":
            want["brigade_count"] = len([k for k in m.kids(gid) if m.groups[k]["kind"] == "brigade"])
        want["post_count"] = len([p for p in m.posts_under(gid) if not m.posts[p]["archived"]])
        got = {k: body.get(k) for k in want}
        if got != want:
            self.fail(("body", name), f"({name}) answer {got} != model {want}")

    def check_post_body(self, name, body, pid):
        m = self.model
        want = m.units_of_post(pid)
        got = ((body.get("corps") or {}).get("id"), (body.get("brigade") or {}).get("id"))
        p = m.posts[pid]
        if got != (want.get("corps"), want.get("brigade")) or body.get("group_id") != p["group"] \
                or body.get("is_deleted") != p["archived"]:
            self.fail(("body", name), f"({name}) post answer units {got} group {body.get('group_id')} deleted "
                                      f"{body.get('is_deleted')} != model {want} {p}")

    async def fetch_all(self, path, params=None, headers=None):
        out, page = [], 1
        while True:
            r = await self.call("GET", path, headers=headers, params={**(params or {}), "page": page, "page_size": 200})
            assert r.status_code == 200, f"{path} {params} -> {r.status_code} {r.text[:200]}"
            data = r.json()
            items = data["items"] if isinstance(data, dict) else data
            out += items
            if len(items) < 200:
                return out
            page += 1

    async def resync(self):
        """After a mismatch the API is taken as truth for groups, posts and the archived flag of observations"""
        m = self.model
        try:
            corps = await self.fetch_all(CORPS, {"include_deleted": "true"})
            brigades = await self.fetch_all(BRIGADES, {"include_deleted": "true"})
            posts = await self.fetch_all(POSTS, {"include_deleted": "true"})
            obs = await self.fetch_all(OBS_LIST, {"include_deleted": "true"})
        except AssertionError:
            return
        for c in corps:
            m.groups[c["id"]] = {"kind": "corps", "parent": None, "archived": c["is_deleted"], "name": c["name"]}
        for b in brigades:
            m.groups[b["id"]] = {"kind": "brigade", "parent": b["corps_id"], "archived": b["is_deleted"], "name": b["name"]}
        for p in posts:
            m.posts[p["id"]] = {"group": p["group_id"], "archived": p["is_deleted"]}
        for o in obs:
            if o["id"] in m.obs:
                m.obs[o["id"]]["archived"] = o["is_deleted"]

    # ---- name helpers
    def pick_name(self):
        base = self.rng.choice(NAME_POOL)
        if self.rng.random() < 0.55:
            self.counter += 1
            base = f"{base}{self.counter}"
        return self.rng.choice([base, base.lower(), base.upper()])

    # ---- generators (return concrete args with symbols)
    def groups_where(self, kind=None, archived=None):
        return [gid for gid, g in self.model.groups.items()
                if (kind is None or g["kind"] == kind) and (archived is None or g["archived"] == archived)]

    def pick_group_target(self, allow_none=True):
        """A group id for a post or a brigade to go to: mostly valid, sometimes archived / wrong / unknown"""
        r = self.rng.random()
        pools = [
            (0.12, lambda: [None] if allow_none else []),
            (0.22, lambda: self.groups_where("corps", False)),
            (0.40, lambda: self.groups_where("brigade", False)),
            (0.08, lambda: self.groups_where("group", False)),
            (0.12, lambda: self.groups_where(archived=True)),
            (0.03, lambda: [UNKNOWN]),
        ]
        acc = 0
        for weight, make in pools:
            acc += weight
            if r < acc:
                options = make()
                if options:
                    return self.sym_for(self.rng.choice(options))
        options = self.groups_where(archived=False) or [None]
        return self.sym_for(self.rng.choice(options))

    def pick_corps_target(self):
        r = self.rng.random()
        for weight, options in ((0.78, self.groups_where("corps", False)), (0.10, self.groups_where("corps", True)),
                                (0.05, self.groups_where("brigade")), (0.04, self.groups_where("group")),
                                (0.03, [UNKNOWN])):
            r -= weight
            if r < 0 and options:
                return self.sym_for(self.rng.choice(options))
        options = self.groups_where("corps", False)
        return self.sym_for(self.rng.choice(options)) if options else UNKNOWN

    def pick_post(self, prefer_active=0.85):
        posts = self.model.posts
        if not posts:
            return None
        active = [p for p, x in posts.items() if not x["archived"]]
        pool = active if (active and self.rng.random() < prefer_active) else list(posts)
        return self.sym_for(self.rng.choice(pool))

    def gen(self, op):
        rng = self.rng
        m = self.model
        if op == "create_corps":
            return {"name": self.pick_name(), "new": self.new_symbol("g")}
        if op == "create_plain":
            plain = [g for g in self.groups_where("group", False) if len(m.chain(g)) < 3]
            parent = self.sym_for(rng.choice(plain)) if plain and rng.random() < 0.4 else None
            return {"parent": parent, "name": self.pick_name(), "new": self.new_symbol("g")}
        if op == "create_brigade":
            return {"corps": self.pick_corps_target(), "name": self.pick_name(), "new": self.new_symbol("g")}
        if op == "create_post":
            self.counter += 1
            return {"group": self.pick_group_target(), "code": f"C{self.counter}", "new": self.new_symbol("p"),
                    "user": rng.random() < 0.3}
        if op == "move_post":
            post = self.pick_post(0.9)
            return None if post is None else {"post": post, "target": self.pick_group_target()}
        if op == "move_brigade":
            pool = self.groups_where("brigade")
            if not pool:
                return None
            if rng.random() < 0.85:
                active = [b for b in pool if not m.groups[b]["archived"]]
                pool = active or pool
            brigade = rng.choice(pool)
            target = self.pick_corps_target()
            if rng.random() < 0.12:
                target = self.sym_for(m.groups[brigade]["parent"])
            return {"brigade": self.sym_for(brigade), "corps": target,
                    "name": self.pick_name() if rng.random() < 0.25 else None}
        if op == "rename":
            pool = self.groups_where("corps") + self.groups_where("brigade")
            if not pool:
                return None
            active = [g for g in pool if not m.groups[g]["archived"]]
            pool = active if active and rng.random() < 0.9 else pool
            gid = rng.choice(pool)
            return {"group": self.sym_for(gid), "kind": m.groups[gid]["kind"], "name": self.pick_name()}
        if op == "archive":
            kind = rng.choice(["corps", "brigade"])
            pool = self.groups_where(kind)
            if not pool:
                return None
            active = [g for g in pool if not m.groups[g]["archived"]]
            pool = active if active and rng.random() < 0.92 else pool
            gid = rng.choice(pool)
            route = kind if rng.random() > 0.06 else ("brigade" if kind == "corps" else "corps")
            return {"group": self.sym_for(gid), "route": route, "cascade": rng.random() < 0.5}
        if op == "restore":
            archived = self.groups_where("corps", True) + self.groups_where("brigade", True)
            pool = archived if archived and rng.random() < 0.85 else (self.groups_where("corps") + self.groups_where("brigade"))
            if not pool:
                return None
            gid = rng.choice(pool)
            kind = m.groups[gid]["kind"]
            route = kind if rng.random() > 0.05 else ("brigade" if kind == "corps" else "corps")
            return {"group": self.sym_for(gid), "route": route}
        if op == "sg_move":
            pool = list(m.groups)
            if not pool:
                return None
            active = [g for g in pool if not m.groups[g]["archived"]]
            pool = active if active and rng.random() < 0.9 else pool
            gid = rng.choice(pool)
            r = rng.random()
            if r < 0.15:
                target = None
            elif r < 0.25:
                target = UNKNOWN
            else:
                options = [g for g in m.groups if g != gid or rng.random() < 0.1] or [None]
                target = self.sym_for(rng.choice(options))
            return {"group": self.sym_for(gid), "target": target}
        if op == "sg_rename":
            pool = list(m.groups)
            if not pool:
                return None
            return {"group": self.sym_for(rng.choice(pool)), "name": self.pick_name()}
        if op == "sg_archive":
            pool = list(m.groups)
            if not pool:
                return None
            empty = [g for g in pool if not m.groups[g]["archived"] and not m.kids(g)
                     and not any(p["group"] == g and not p["archived"] for p in m.posts.values())]
            pool = empty if empty and rng.random() < 0.6 else pool
            return {"group": self.sym_for(rng.choice(pool))}
        if op == "sg_restore":
            archived = self.groups_where(archived=True)
            pool = archived if archived and rng.random() < 0.85 else list(m.groups)
            return {"group": self.sym_for(rng.choice(pool))} if pool else None
        if op in ("post_archive", "post_restore"):
            pool = [p for p, x in m.posts.items() if x["archived"] == (op == "post_restore")] or list(m.posts)
            return {"post": self.sym_for(rng.choice(pool))} if pool else None
        if op == "obs_create":
            post = self.pick_post(0.93)
            return None if post is None else {"post": post, "new": self.new_symbol("o"), "media": rng.random() < 0.3}
        if op == "obs_update":
            pool = list(m.obs)
            return {"obs": self.sym_for(rng.choice(pool)), "comment": f"c{rng.randint(1, 99)}"} if pool else None
        if op in ("obs_archive", "obs_restore"):
            want = op == "obs_restore"
            pool = [o for o, x in m.obs.items() if x["archived"] == want] or list(m.obs)
            return {"obs": self.sym_for(rng.choice(pool))} if pool else None
        raise AssertionError(op)

    # ---- executors (predict, call, update the model on success)
    async def do(self, op, a):
        try:
            await getattr(self, "do_" + op)(a)
        except Skip:
            STATS["ops_skipped"] += 1

    async def do_create_corps(self, a):
        m = self.model
        taken = m.name_taken(None, a["name"])

        def ok(body):
            m.groups[body["id"]] = {"kind": "corps", "parent": None, "archived": False, "name": body["name"]}
            self.bind(a["new"], body["id"])
        await self.step("create_corps", "POST", CORPS, (409, "scope_group_name_taken") if taken else 201,
                        on_ok=ok, unit=lambda: self.ref(a["new"]), json={"name": a["name"]})

    async def do_create_plain(self, a):
        m = self.model
        parent = self.ref(a["parent"]) if a["parent"] is not None else None
        taken = m.name_taken(parent, a["name"])

        def ok(body):
            m.groups[body["id"]] = {"kind": "group", "parent": parent, "archived": False, "name": body["name"]}
            self.bind(a["new"], body["id"])
        await self.step("create_plain", "POST", GROUPS, (409, "scope_group_name_taken") if taken else 201,
                        on_ok=ok, json={"name": a["name"], "parent_id": parent})

    async def do_create_brigade(self, a):
        m = self.model
        cid = self.ref(a["corps"])
        g = m.groups.get(cid)
        if g is None:
            expect = (404, "scope_group_not_found")
        elif g["kind"] != "corps":
            expect = (409, "scope_group_parent_not_allowed")
        elif g["archived"]:
            expect = (404, "scope_group_not_found")
        elif m.name_taken(cid, a["name"]):
            expect = (409, "scope_group_name_taken")
        else:
            expect = 201

        def ok(body):
            m.groups[body["id"]] = {"kind": "brigade", "parent": cid, "archived": False, "name": body["name"]}
            self.bind(a["new"], body["id"])
        await self.step("create_brigade", "POST", BRIGADES, expect, on_ok=ok, unit=lambda: self.ref(a["new"]),
                        json={"corps_id": cid, "name": a["name"]})

    def group_expect(self, gid):
        """Post create / move into this group (None: the root)"""
        if gid is None:
            return 200
        g = self.model.groups.get(gid)
        return (404, "scope_group_not_found") if g is None or g["archived"] else 200

    async def do_create_post(self, a):
        m = self.model
        gid = self.ref(a["group"]) if a["group"] is not None else None

        def ok(body):
            m.posts[body["id"]] = {"group": gid, "archived": False}
            self.bind(a["new"], body["id"])
        body = await self.step("create_post", "POST", POSTS, self.group_expect(gid), on_ok=ok,
                               post=lambda: self.ref(a["new"]), json={"name": f"Post {a['code']}", "code": a["code"], "group_id": gid})
        if body is not None and a.get("user"):
            user = await make_user(self.env.db, post_id=body["id"])
            m.users[user.id] = body["id"]

    async def do_move_post(self, a):
        m = self.model
        pid = self.ref(a["post"])
        target = self.ref(a["target"]) if a["target"] is not None else None
        p = m.posts[pid]
        if p["archived"]:
            expect = (404, "post_not_found")
        elif target == p["group"]:
            expect = 200
        else:
            expect = self.group_expect(target)

        def ok(body):
            p["group"] = target
        await self.step("move_post", "PUT", V1.path(PostRoutes.POST_BY_ID, post_id=pid), expect, on_ok=ok,
                        post=pid, json={"group_id": target})

    async def do_move_brigade(self, a):
        m = self.model
        bid = self.ref(a["brigade"])
        cid = self.ref(a["corps"])
        b = m.groups[bid]
        if b["archived"]:
            expect = (404, "scope_group_not_found")
        else:
            expect = 200
            if cid != b["parent"]:
                t = m.groups.get(cid)
                if t is None:
                    expect = (404, "scope_group_not_found")
                elif t["kind"] != "corps":
                    expect = (409, "scope_group_parent_not_allowed")
                elif t["archived"]:
                    expect = (404, "scope_group_not_found")
            if expect == 200 and m.name_taken(cid, a["name"] or b["name"], exclude=bid):
                expect = (409, "scope_group_name_taken")
        body = {"corps_id": cid}
        if a["name"]:
            body["name"] = a["name"]

        def ok(res):
            b["parent"] = cid
            b["name"] = res["name"]
        await self.step("move_brigade", "PATCH", V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=bid), expect,
                        on_ok=ok, unit=bid, json=body)

    async def do_rename(self, a):
        m = self.model
        gid = self.ref(a["group"])
        g = m.groups[gid]
        if g["archived"]:
            expect = (404, "scope_group_not_found")
        elif m.name_taken(g["parent"], a["name"], exclude=gid):
            expect = (409, "scope_group_name_taken")
        else:
            expect = 200
        path = (V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=gid) if a["kind"] == "corps"
                else V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=gid))

        def ok(res):
            g["name"] = res["name"]
        await self.step("rename", "PATCH", path, expect, on_ok=ok, unit=gid, json={"name": a["name"]})

    async def do_archive(self, a):
        m = self.model
        gid = self.ref(a["group"])
        g = m.groups[gid]
        route, cascade = a["route"], a["cascade"]
        if g["kind"] != route or g["archived"]:
            expect = (404, "scope_group_not_found")
        elif route == "corps" and not cascade and m.kids(gid):
            expect = (409, "corps_has_brigades")
        else:
            expect = 200

        def ok(res):
            kids = m.kids(gid)
            holders = [gid, *kids]
            for p in m.posts.values():
                if p["group"] in holders and not p["archived"]:
                    if cascade:
                        p["archived"] = True
                    else:
                        p["group"] = g["parent"]
            for k in (*kids, gid):
                m.groups[k]["archived"] = True
        path = (V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=gid) if route == "corps"
                else V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=gid))
        await self.step("archive_" + route, "POST", path, expect, on_ok=ok, unit=gid,
                        params={"cascade": "true" if cascade else "false"})

    async def do_restore(self, a):
        m = self.model
        gid = self.ref(a["group"])
        g = m.groups[gid]
        route = a["route"]
        if g["kind"] != route:
            expect = (404, "scope_group_not_found")
        elif not g["archived"]:
            expect = (409, "scope_group_not_archived")
        elif g["parent"] is not None and m.groups[g["parent"]]["archived"]:
            expect = (404, "scope_group_not_found")
        else:
            expect = 200

        def ok(res):
            g["archived"] = False
        path = (V1.path(CorpsRoutes.CORPS_RESTORE, corps_id=gid) if route == "corps"
                else V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=gid))
        await self.step("restore_" + route, "POST", path, expect, on_ok=ok, unit=gid)

    def tree_check(self, gid, target):
        """check_new_parent + check_placement for a group going under target (None: the root)"""
        m = self.model
        if target is None:
            return None
        t = m.groups.get(target)
        if t is None or t["archived"]:
            return (404, "scope_group_not_found")
        if gid in m.chain(target):
            return (409, "scope_group_cycle")
        height = m.height(gid) if not m.groups[gid]["archived"] else 1
        if len(m.chain(target)) + height > 6:
            return (409, "scope_group_depth_exceeded")
        allowed = {"group": {None, "group"}, "corps": {None}, "brigade": {"corps"}}[m.groups[gid]["kind"]]
        if t["kind"] not in allowed:
            return (409, "scope_group_parent_not_allowed")
        return None

    async def do_sg_move(self, a):
        m = self.model
        gid = self.ref(a["group"])
        target = self.ref(a["target"]) if a["target"] is not None else None
        g = m.groups[gid]
        expect = 200
        if g["archived"]:
            expect = (404, "scope_group_not_found")
        else:
            if target != g["parent"]:
                expect = self.tree_check(gid, target) or 200
                if expect == 200 and target is None and g["kind"] != "group" and g["kind"] != "corps":
                    expect = (409, "scope_group_parent_not_allowed")
            if expect == 200 and m.name_taken(target, g["name"], exclude=gid):
                expect = (409, "scope_group_name_taken")

        def ok(res):
            g["parent"] = target
        await self.step("sg_move", "PATCH", V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=gid), expect, on_ok=ok,
                        json={"parent_id": target})

    async def do_sg_rename(self, a):
        m = self.model
        gid = self.ref(a["group"])
        g = m.groups[gid]
        if g["archived"]:
            expect = (404, "scope_group_not_found")
        elif m.name_taken(g["parent"], a["name"], exclude=gid):
            expect = (409, "scope_group_name_taken")
        else:
            expect = 200

        def ok(res):
            g["name"] = res["name"]
        await self.step("sg_rename", "PATCH", V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=gid), expect, on_ok=ok,
                        json={"name": a["name"]})

    async def do_sg_archive(self, a):
        m = self.model
        gid = self.ref(a["group"])
        g = m.groups[gid]
        if g["archived"]:
            expect = (404, "scope_group_not_found")
        elif m.kids(gid) or any(p["group"] == gid and not p["archived"] for p in m.posts.values()):
            expect = (409, "scope_group_not_empty")
        else:
            expect = 200

        def ok(res):
            g["archived"] = True
        await self.step("sg_archive", "POST", V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=gid), expect, on_ok=ok)

    async def do_sg_restore(self, a):
        m = self.model
        gid = self.ref(a["group"])
        g = m.groups[gid]
        if not g["archived"]:
            expect = (409, "scope_group_not_archived")
        else:
            expect = self.tree_check(gid, g["parent"]) or 200

        def ok(res):
            g["archived"] = False
        await self.step("sg_restore", "POST", V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=gid), expect, on_ok=ok)

    async def do_post_archive(self, a):
        pid = self.ref(a["post"])
        p = self.model.posts[pid]
        expect = (404, "post_not_found") if p["archived"] else 200

        def ok(res):
            p["archived"] = True
        await self.step("post_archive", "POST", V1.path(PostRoutes.POST_ARCHIVE, post_id=pid), expect, on_ok=ok, post=pid)

    async def do_post_restore(self, a):
        m = self.model
        pid = self.ref(a["post"])
        p = m.posts[pid]
        if not p["archived"]:
            expect = (409, "post_not_archived")
        elif p["group"] is not None and m.groups[p["group"]]["archived"]:
            expect = (404, "scope_group_not_found")
        else:
            expect = 200

        def ok(res):
            p["archived"] = False
        await self.step("post_restore", "POST", V1.path(PostRoutes.POST_RESTORE, post_id=pid), expect, on_ok=ok, post=pid)

    async def do_obs_create(self, a):
        m = self.model
        pid = self.ref(a["post"])
        p = m.posts[pid]
        expect = (409, "post_archived") if p["archived"] else 200
        units = m.units_of_group(p["group"])

        def ok(res):
            m.obs[res["id"]] = {"post": pid, "corps": units.get("corps"), "brigade": units.get("brigade"), "archived": False}
            self.bind(a["new"], res["id"])
            got = ((res.get("corps") or {}).get("id"), (res.get("brigade") or {}).get("id"))
            if got != (units.get("corps"), units.get("brigade")):
                self.fail(("create-response-units",), f"observation create response units {got} != model "
                                                      f"{(units.get('corps'), units.get('brigade'))}")
        body = await self.step("obs_create", "POST", OBS_CREATE, expect, on_ok=ok,
                               json=observation_payload(pid, self.env.dictionary["signal_type_id"]))
        if body is not None and a.get("media"):
            row = await self.env.db.get(Observation, body["id"])
            media = await make_media(self.env.db, row, self.env.admin, "image")
            m.media[media.id] = body["id"]

    async def do_obs_update(self, a):
        m = self.model
        oid = self.ref(a["obs"])
        o = m.obs[oid]
        if m.posts[o["post"]]["archived"]:
            expect = (409, "post_archived")
        elif o["archived"]:
            expect = (404, "observation_not_found")
        else:
            expect = 200

        def ok(res):
            got = ((res.get("corps") or {}).get("id"), (res.get("brigade") or {}).get("id"))
            if got != (o["corps"], o["brigade"]):
                self.fail(("body", "obs_update-units"), f"observation update answer units {got} != recorded "
                                                        f"{(o['corps'], o['brigade'])}")
        await self.step("obs_update", "PUT", V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=oid), expect,
                        on_ok=ok, json={"comment": a["comment"]})

    async def do_obs_archive(self, a):
        oid = self.ref(a["obs"])
        o = self.model.obs[oid]
        expect = (404, "observation_not_found") if o["archived"] else 200

        def ok(res):
            o["archived"] = True
        await self.step("obs_archive", "POST", V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=oid),
                        expect, on_ok=ok)

    async def do_obs_restore(self, a):
        oid = self.ref(a["obs"])
        o = self.model.obs[oid]
        expect = 200 if o["archived"] else (409, "observation_not_archived")

        def ok(res):
            o["archived"] = False
        await self.step("obs_restore", "POST", V1.path(ObservationRoutes.OBSERVATION_RESTORE, observation_id=oid),
                        expect, on_ok=ok)

    # ---- the random driver
    PRELUDE = ["create_corps", "create_corps", "create_brigade", "create_brigade", "create_post", "create_post",
               "create_post", "obs_create", "obs_create"]
    WEIGHTS = [("create_corps", 7), ("create_plain", 1), ("create_brigade", 8), ("create_post", 10), ("move_post", 12),
               ("move_brigade", 6), ("rename", 4), ("archive", 11), ("restore", 9), ("post_archive", 3),
               ("post_restore", 3), ("obs_create", 14), ("obs_archive", 3), ("obs_restore", 2), ("obs_update", 2),
               ("sg_move", 3), ("sg_rename", 1), ("sg_archive", 2), ("sg_restore", 2)]

    def next_op(self, index):
        if index < len(self.PRELUDE):
            return self.PRELUDE[index]
        ops, weights = zip(*self.WEIGHTS)
        return self.rng.choices(ops, weights)[0]

    async def run_random(self, n_ops):
        light_every = 4
        heavy_every = 15
        viewer_at = self.rng.randint(8, max(9, n_ops // 2))
        for i in range(n_ops):
            self.op_no = i
            op = self.next_op(i)
            args = self.gen(op)
            if args is None:
                continue
            self.script.append((op, args))
            STATS["ops"] += 1
            await self.do(op, args)
            if self.want_viewer and self.viewer is None and i >= viewer_at:
                await self.make_viewer()
            if (i + 1) % light_every == 0:
                await self.checks(heavy=(i + 1) % heavy_every == 0)
        await self.checks(heavy=True)

    async def run_script(self, script):
        for i, (op, args) in enumerate(script):
            self.op_no = i
            self.script.append((op, args))
            await self.do(op, args)
        await self.checks(heavy=True)

    # ---- checks
    async def checks(self, heavy):
        for name, fn in (("light", self.check_light), ("heavy", self.check_heavy), ("viewer", self.check_viewer)):
            if name == "heavy" and not heavy:
                continue
            if name == "viewer" and (self.viewer is None or not heavy):
                continue
            try:
                await fn()
            except AssertionError as exc:
                self.fail(("check-error", name, str(exc)[:80]), f"check {name} could not run: {exc}")
            except Exception as exc:  # noqa: BLE001
                self.fail(("check-exception", name, type(exc).__name__),
                          f"check {name} raised {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}")
        STATS["light_checks"] += 1
        STATS["heavy_checks"] += 1 if heavy else 0

    def units_at(self, post_id):
        return self.model.units_of_post(post_id)

    async def check_light(self):
        m = self.model
        # API state
        corps = await self.fetch_all(CORPS, {"include_deleted": "true"})
        brigades = await self.fetch_all(BRIGADES, {"include_deleted": "true"})
        posts = await self.fetch_all(POSTS, {"include_deleted": "true"})
        obs = await self.fetch_all(OBS_LIST, {"include_deleted": "true"})
        api_groups = {c["id"]: ("corps", None, c["is_deleted"], c["name"]) for c in corps}
        api_groups.update({b["id"]: ("brigade", b["corps_id"], b["is_deleted"], b["name"]) for b in brigades})
        want_groups = {g: (v["kind"], v["parent"], v["archived"], v["name"]) for g, v in m.groups.items()
                       if v["kind"] in ("corps", "brigade")}
        if api_groups != want_groups:
            diff = {k: (want_groups.get(k), api_groups.get(k)) for k in set(want_groups) | set(api_groups)
                    if want_groups.get(k) != api_groups.get(k)}
            self.fail(("light", "groups-state"), f"units list differs from model (model, api): {diff}")
        api_posts = {p["id"]: (p["group_id"], p["is_deleted"]) for p in posts}
        want_posts = {p: (v["group"], v["archived"]) for p, v in m.posts.items()}
        if api_posts != want_posts:
            diff = {k: (want_posts.get(k), api_posts.get(k)) for k in set(want_posts) | set(api_posts)
                    if want_posts.get(k) != api_posts.get(k)}
            self.fail(("light", "posts-state"), f"posts differ from model (model, api): {diff}")
        everything = await self.fetch_all(GROUPS, {"include_deleted": "true"})
        api_all = {g["id"]: (g["kind"], g["parent_id"], g["is_deleted"], g["name"]) for g in everything}
        want_all = {g: (v["kind"], v["parent"], v["archived"], v["name"]) for g, v in m.groups.items()}
        if api_all != want_all:
            diff = {k: (want_all.get(k), api_all.get(k)) for k in set(want_all) | set(api_all)
                    if want_all.get(k) != api_all.get(k)}
            self.fail(("light", "scope-groups-state"), f"GET /scope-groups differs from model (model, api): {diff}")
        # (a) no active post / brigade inside an archived group, from the API's own data
        archived_ids = {g for g, v in api_groups.items() if v[2]}
        for g, (kind, parent, is_deleted, _) in api_groups.items():
            if not is_deleted and parent in archived_ids:
                self.fail(("a", "active-brigade-in-archived-corps"), f"(a) active brigade {g} in archived corps {parent}")
        plain_archived = {g for g, v in m.groups.items() if v["archived"] and v["kind"] == "group"}
        for p in posts:
            if not p["is_deleted"] and (p["group_id"] in archived_ids or p["group_id"] in plain_archived):
                self.fail(("a", "active-post-in-archived-group"), f"(a) active post {p['id']} in archived group {p['group_id']}")
        # (b) units of posts
        for p in posts:
            if p["id"] not in m.posts:
                continue
            want = self.units_at(p["id"])
            got = {"corps": (p.get("corps") or {}).get("id"), "brigade": (p.get("brigade") or {}).get("id")}
            if got != {"corps": want.get("corps"), "brigade": want.get("brigade")}:
                self.fail(("b", "post-units"), f"(b) post {p['id']} units {got} != model {want}")
            for kind in ("corps", "brigade"):
                ref = p.get(kind)
                if ref and ref.get("name") != m.groups[ref["id"]]["name"]:
                    self.fail(("b", "post-unit-name"), f"(b) post {p['id']} {kind} name {ref} != model")
        # (c) recorded units of observations
        for o in obs:
            if o["id"] not in m.obs:
                continue
            mo = m.obs[o["id"]]
            got = ((o.get("corps") or {}).get("id"), (o.get("brigade") or {}).get("id"))
            if got != (mo["corps"], mo["brigade"]):
                self.fail(("c", "recorded-units"), f"(c) observation {o['id']} units {got} != model "
                                                   f"{(mo['corps'], mo['brigade'])}")
            if o["is_deleted"] != mo["archived"]:
                self.fail(("c", "obs-archived"), f"(c) observation {o['id']} is_deleted {o['is_deleted']} != model")
        if {o["id"] for o in obs} != set(m.obs):
            self.fail(("c", "obs-set"), "(c) observation ids differ from model")
        # (f) counts on active units
        for gid, g in m.groups.items():
            if g["kind"] not in ("corps", "brigade"):
                continue
            path = (V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=gid) if g["kind"] == "corps"
                    else V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=gid))
            r = await self.call("GET", path)
            if g["archived"]:
                if r.status_code != 404:
                    self.fail(("f", "archived-get"), f"(f) GET archived {g['kind']} {gid} -> {r.status_code}")
                continue
            if r.status_code != 200:
                self.fail(("f", "get-status"), f"(f) GET active {g['kind']} {gid} -> {r.status_code}")
                continue
            body = r.json()
            active_posts = [p for p in m.posts_under(gid) if not m.posts[p]["archived"]]
            if body["post_count"] != len(active_posts):
                self.fail(("f", g["kind"] + "-post_count"), f"(f) {g['kind']} {gid} post_count {body['post_count']} != {len(active_posts)}")
            if g["kind"] == "corps":
                want_b = len([k for k in m.kids(gid) if m.groups[k]["kind"] == "brigade"])
                if body["brigade_count"] != want_b:
                    self.fail(("f", "brigade_count"), f"(f) corps {gid} brigade_count {body['brigade_count']} != {want_b}")
        STATS["light_checks"] += 0

    async def list_ids(self, params, headers=None):
        """list-all and search return the same set and the search total matches"""
        listed = await self.fetch_all(OBS_LIST, params, headers=headers)
        found = await self.call("GET", OBS_SEARCH, headers=headers, params={**params, "page_size": 200})
        assert found.status_code == 200, f"search {params} -> {found.status_code} {found.text[:200]}"
        data = found.json()
        ids = {o["id"] for o in listed}
        if {o["id"] for o in data["items"]} != ids or data["total"] != len(ids):
            self.fail(("d", "search-vs-list"), f"(d) search differs from list for {params}: list {sorted(ids)} "
                                               f"search {sorted(o['id'] for o in data['items'])} total {data['total']}")
        return ids

    def unit_param(self, kind, uid):
        return {f"{kind}_id": uid}

    async def check_heavy(self):
        m = self.model
        units = m.unit_ids()
        for gid in units:
            kind = m.groups[gid]["kind"]
            for placement in ("recorded", "current"):
                for include_deleted in ((False, True) if self.rng.random() < 0.35 else (False,)):
                    want = m.obs_expected({kind: gid}, placement, include_deleted)
                    params = {**self.unit_param(kind, gid), "placement": placement}
                    if include_deleted:
                        params["include_deleted"] = "true"
                    got = await self.list_ids(params)
                    if got != want:
                        self.fail(("d", kind, placement, "deleted" if include_deleted else "live"),
                                  f"(d) {kind}_id={gid} placement={placement} include_deleted={include_deleted}: "
                                  f"api {sorted(got)} model {sorted(want)}")
                # (e) stats
                path = (V1.path(CorpsRoutes.CORPS_OBSERVATION_STATS, corps_id=gid) if kind == "corps"
                        else V1.path(BrigadeRoutes.BRIGADE_OBSERVATION_STATS, brigade_id=gid))
                r = await self.call("GET", path, params={"tz": "UTC", "placement": placement, "include_archived": "true"})
                if r.status_code != 200:
                    self.fail(("e", "status"), f"(e) stats {kind} {gid} -> {r.status_code} {r.text[:200]}")
                else:
                    allo = m.obs_expected({kind: gid}, placement, True)
                    live = {o for o in allo if not m.obs[o]["archived"]}
                    at = r.json()["all_time"]
                    for window in ("last_24h", "period"):
                        w = r.json()[window]
                        if (w["not_archived"], w["archived"]) != (at["not_archived"], at["archived"]):
                            self.fail(("e", window, kind, placement), f"(e) stats {kind}_id={gid} {placement}: {window} "
                                                                      f"{(w['not_archived'], w['archived'])} != all_time {(at['not_archived'], at['archived'])}")
                    if (at["not_archived"], at["archived"]) != (len(live), len(allo) - len(live)):
                        self.fail(("e", kind, placement), f"(e) stats {kind}_id={gid} {placement}: api "
                                                          f"{(at['not_archived'], at['archived'])} model {(len(live), len(allo) - len(live))}")
        # cross-kind and combined filters
        corps = m.unit_ids()
        for _ in range(4):
            if not corps:
                break
            a, b = self.rng.choice(corps), self.rng.choice(corps)
            placement = self.rng.choice(["recorded", "current"])
            ka, kb = m.groups[a]["kind"], m.groups[b]["kind"]
            if ka == kb:
                continue
            want = m.obs_expected({ka: a, kb: b}, placement, False)
            params = {f"{ka}_id": a, f"{kb}_id": b, "placement": placement}
            got = await self.list_ids(params)
            if got != want:
                self.fail(("d", "combined", placement), f"(d) {params}: api {sorted(got)} model {sorted(want)}")
        wrong = [g for g in m.groups if m.groups[g]["kind"] == "brigade"][:1]
        for g in wrong:
            got = await self.list_ids({"corps_id": g, "placement": "recorded"})
            if got:
                self.fail(("d", "wrong-kind"), f"(d) corps_id of a brigade ({g}) gave {sorted(got)}")
        await self.check_extra(self.h)
        # (g) report
        sample = self.rng.sample(units, min(2, len(units)))
        for gid in sample:
            kind = m.groups[gid]["kind"]
            for placement in ("recorded", "current"):
                want = len(m.obs_expected({kind: gid}, placement, False))
                r = await self.call("POST", OBS_REPORT, json={"report_type": "all_fields", f"{kind}_ids": [gid], "placement": placement})
                STATS["report_checks"] += 1
                if r.status_code != 200:
                    self.fail(("g", "status"), f"(g) report {kind}_ids=[{gid}] {placement} -> {r.status_code} {r.text[:200]}")
                    continue
                rows = list(openpyxl.load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True))
                at = next((i for i, row in enumerate(rows) if row[0] == USER_HEADER), None)
                if at is None:
                    self.fail(("g", "no-header"), f"(g) report {kind}_ids=[{gid}] has no header row")
                    continue
                got = len([row for row in rows[at + 1:] if any(c is not None for c in row)])
                if got != want:
                    self.fail(("g", kind, placement), f"(g) report {kind}_ids=[{gid}] {placement}: {got} rows, model {want}")


    async def check_extra(self, headers, accessible=None, node=None):
        """Post / user / media filters and the sublists of a unit; accessible: the posts the caller sees (None: all)"""
        m = self.model
        admin = accessible is None
        units = m.unit_ids()
        for g in self.rng.sample(units, min(4, len(units))):
            kind = m.groups[g]["kind"]
            under = m.posts_under(g)
            if accessible is not None:
                under = under & accessible
            live = {p for p in under if not m.posts[p]["archived"]}
            for include_deleted, want in [(False, live)] + ([(True, under)] if admin else []):
                params = {f"{kind}_id": g, **({"include_deleted": "true"} if include_deleted else {})}
                for label, path in (("posts", POSTS), ("search-posts", POSTS_SEARCH)):
                    got = {p["id"] for p in await self.fetch_all(path, params, headers)}
                    if got != want:
                        self.fail(("x", label, kind, include_deleted), f"(x) {path} {params}: api {sorted(got)} model {sorted(want)}")
            # sublists of one unit
            visible = (not m.groups[g]["archived"]) if admin else (
                m.groups[node]["archived"] is False and node in m.chain(g) and not m.groups[g]["archived"])
            sub = (V1.path(CorpsRoutes.CORPS_POSTS, corps_id=g) if kind == "corps"
                   else V1.path(BrigadeRoutes.BRIGADE_POSTS, brigade_id=g))
            for include_deleted in ((False, True) if admin else (False,)):
                params = {"include_deleted": "true"} if include_deleted else {}
                r = await self.call("GET", sub, headers=headers, params={**params, "page_size": 200})
                if not visible:
                    if r.status_code != 404:
                        self.fail(("x", "unit-posts-hidden"), f"(x) GET {sub} {params} -> {r.status_code}, model: not visible")
                    continue
                if r.status_code != 200:
                    self.fail(("x", "unit-posts-status"), f"(x) GET {sub} {params} -> {r.status_code} {r.text[:150]}")
                    continue
                want = under if include_deleted else live
                got = {p["id"] for p in r.json()}
                if got != want:
                    self.fail(("x", "unit-posts", kind, include_deleted), f"(x) {sub} {params}: api {sorted(got)} model {sorted(want)}")
            if kind == "corps" and visible:
                for include_deleted in ((False, True) if admin else (False,)):
                    params = {"include_deleted": "true"} if include_deleted else {}
                    r = await self.call("GET", V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=g), headers=headers,
                                        params={**params, "page_size": 200})
                    r2 = await self.call("GET", BRIGADES, headers=headers, params={**params, "corps_id": g, "page_size": 200})
                    want = set(m.kids(g, active=not include_deleted))
                    for label, resp in (("corps-brigades", r), ("brigades-corps_id", r2)):
                        got = {b["id"] for b in resp.json()} if resp.status_code == 200 else None
                        if got != want:
                            self.fail(("x", label, include_deleted), f"(x) {label} corps {g} {params}: {resp.status_code} api {got and sorted(got)} model {sorted(want)}")
            if admin:
                want = {u for u, hp in m.users.items() if hp in m.posts_under(g)}
                got = {u["id"] for u in await self.fetch_all(USERS, {f"{kind}_id": g, "membership": "home"}, headers)}
                if got != want:
                    self.fail(("x", "users-filter", kind), f"(x) users {kind}_id={g}: api {sorted(got)} model {sorted(want)}")
                for placement in ("recorded", "current"):
                    want = {mid for mid, o in m.media.items() if o in m.obs_expected({kind: g}, placement, False)}
                    for label, path in (("media", MEDIA), ("search-media", V1.path(SearchRoutes.MEDIA))):
                        got = {x["id"] for x in await self.fetch_all(path, {f"{kind}_id": g, "placement": placement}, headers)}
                        if got != want:
                            self.fail(("x", label, kind, placement), f"(x) {path} {kind}_id={g} {placement}: api {sorted(got)} model {sorted(want)}")

    # ---- viewer
    async def make_viewer(self):
        m = self.model
        pool = [g for g in m.unit_ids() if not m.groups[g]["archived"]]
        if not pool:
            return
        node = self.rng.choice(pool)
        db = self.env.db
        user = await make_user(db)
        role = await make_role(db, [Permissions.OBSERVATION_VIEW, Permissions.POST_VIEW, Permissions.SCOPE_GROUP_VIEW,
                                    Permissions.EXPORT_CREATE])
        await assign(db, user, role, Node.group(node))
        self.viewer = (user, self.env.headers(user), node)
        self.script.append(("viewer", {"node": self.sym_for(node)}))

    async def check_viewer(self):
        STATS["viewer_checks"] += 1
        m = self.model
        user, vh, node = self.viewer
        node_active = not m.groups[node]["archived"]
        if not node_active:
            # a role at an archived group no longer applies: the viewer holds nothing, 403 on every unit route
            for path in (CORPS, BRIGADES, POSTS):
                r = await self.call("GET", path, headers=vh)
                if r.status_code != 403:
                    self.fail(("v", "archived-node-access"), f"(v) viewer at archived {m.groups[node]['kind']} {node}: GET {path} -> {r.status_code}")
            return
        accessible = m.posts_under(node) if node_active else set()
        # lists of units: exactly the active units inside the node
        corps = {c["id"] for c in await self.fetch_all(CORPS, headers=vh)}
        brigades = {b["id"] for b in await self.fetch_all(BRIGADES, headers=vh)}
        inside = [g for g in m.unit_ids() if node in m.chain(g)] if node_active else []
        want_corps = {g for g in inside if m.groups[g]["kind"] == "corps" and not m.groups[g]["archived"]}
        want_brigades = {g for g in inside if m.groups[g]["kind"] == "brigade" and not m.groups[g]["archived"]}
        if corps != want_corps:
            self.fail(("v", "corps-list"), f"(v) viewer at {m.groups[node]['kind']} {node}: GET /corps ids {sorted(corps)} model {sorted(want_corps)}")
        if brigades != want_brigades:
            self.fail(("v", "brigades-list"), f"(v) viewer at {m.groups[node]['kind']} {node}: GET /brigades ids {sorted(brigades)} model {sorted(want_brigades)}")
        # posts list
        posts = await self.fetch_all(POSTS, headers=vh)
        want_posts = {p for p in accessible if not m.posts[p]["archived"]}
        if {p["id"] for p in posts} != want_posts:
            self.fail(("v", "posts-list"), f"(v) viewer posts {sorted(p['id'] for p in posts)} model {sorted(want_posts)}")
        for p in posts:
            want = m.units_of_post(p["id"]) if p["id"] in m.posts else {}
            if (p.get("corps") or {}).get("id") != want.get("corps") or (p.get("brigade") or {}).get("id") != want.get("brigade"):
                self.fail(("v", "post-units"), f"(v) viewer post {p['id']} units differ from model")
        # unit detail: visible iff active and inside the node
        for g in m.unit_ids():
            kind = m.groups[g]["kind"]
            path = (V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=g) if kind == "corps"
                    else V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=g))
            r = await self.call("GET", path, headers=vh)
            visible = node_active and node in m.chain(g) and not m.groups[g]["archived"]
            if (r.status_code == 200) != visible:
                self.fail(("v", "unit-detail"), f"(v) viewer GET {kind} {g} -> {r.status_code}, model visible={visible}")
            elif visible:
                body = r.json()
                active_posts = [p for p in m.posts_under(g) if not m.posts[p]["archived"]]
                if body["post_count"] != len(active_posts):
                    self.fail(("v", "unit-post_count"), f"(v) viewer {kind} {g} post_count {body['post_count']} != {len(active_posts)}")
        # observations: the model intersected with what the node covers now
        base = {o for o, x in m.obs.items() if not x["archived"] and x["post"] in accessible}
        got = await self.list_ids({}, headers=vh)
        if got != base:
            self.fail(("v", "obs-all"), f"(v) viewer all observations {sorted(got)} model {sorted(base)}")
        for g in m.unit_ids():
            kind = m.groups[g]["kind"]
            for placement in ("recorded", "current"):
                want = {o for o in m.obs_expected({kind: g}, placement, False) if m.obs[o]["post"] in accessible}
                got = await self.list_ids({f"{kind}_id": g, "placement": placement}, headers=vh)
                if got != want:
                    self.fail(("v", "obs-filter", kind, placement),
                              f"(v) viewer at {m.groups[node]['kind']} {node}: {kind}_id={g} {placement}: api {sorted(got)} model {sorted(want)}")
                if node_active:
                    path = (V1.path(CorpsRoutes.CORPS_OBSERVATION_STATS, corps_id=g) if kind == "corps"
                            else V1.path(BrigadeRoutes.BRIGADE_OBSERVATION_STATS, brigade_id=g))
                    r = await self.call("GET", path, headers=vh, params={"tz": "UTC", "placement": placement})
                    inside_node = node in m.chain(g)
                    if (r.status_code == 200) != inside_node:
                        self.fail(("v", "stats-status"), f"(v) viewer stats {kind} {g} -> {r.status_code}, inside node: {inside_node}")
                    elif inside_node and r.json()["all_time"]["not_archived"] != len(want):
                        self.fail(("v", "stats-count", kind, placement),
                                  f"(v) viewer stats {kind}_id={g} {placement}: {r.json()['all_time']['not_archived']} model {len(want)}")
        await self.check_extra(vh, accessible, node)
        if self.rng.random() < 0.3:
            sample = self.rng.sample(m.unit_ids(), min(1, len(m.unit_ids())))
            for g in sample:
                kind = m.groups[g]["kind"]
                for placement in ("recorded", "current"):
                    want = len({o for o in m.obs_expected({kind: g}, placement, False) if m.obs[o]["post"] in accessible})
                    r = await self.call("POST", OBS_REPORT, headers=vh,
                                        json={"report_type": "all_fields", f"{kind}_ids": [g], "placement": placement})
                    if r.status_code != 200:
                        self.fail(("v", "report-status"), f"(v) viewer report {kind}_ids=[{g}] -> {r.status_code} {r.text[:150]}")
                        continue
                    rows = list(openpyxl.load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True))
                    at = next((i for i, row in enumerate(rows) if row[0] == USER_HEADER), None)
                    cnt = len([row for row in rows[at + 1:] if any(c is not None for c in row)]) if at is not None else -1
                    if cnt != want:
                        self.fail(("v", "report-count", kind, placement), f"(v) viewer report {kind}_ids=[{g}] {placement}: {cnt} rows, model {want}")


# ---------------------------------------------------------------------------------------------- minimisation
async def replay_keys(script, viewer):
    """Run a script in a fresh environment; the failure keys it produces"""
    async with fresh_env() as env:
        sim = Sim(env, 0, viewer=False)
        sim.rng = random.Random(1)
        # the viewer entry (if kept) is created when reached
        for i, (op, args) in enumerate(script):
            sim.op_no = i
            sim.script.append((op, args))
            if op == "viewer":
                try:
                    node = sim.ref(args["node"])
                except Skip:
                    continue
                if node in sim.model.groups and not sim.model.groups[node]["archived"]:
                    db = env.db
                    user = await make_user(db)
                    role = await make_role(db, [Permissions.OBSERVATION_VIEW, Permissions.POST_VIEW,
                                                Permissions.SCOPE_GROUP_VIEW, Permissions.EXPORT_CREATE])
                    await assign(db, user, role, Node.group(node))
                    sim.viewer = (user, env.headers(user), node)
                continue
            await sim.do(op, args)
        await sim.checks(heavy=True)
        return set(sim.fails), sim.log


async def minimise(script, key, viewer, deadline):
    """Delta debugging over the ops of a script, keeping the one failure key"""
    cache = {}

    async def fails(sub):
        marker = tuple(id(x) for x in sub)
        if marker in cache:
            return cache[marker]
        if time.time() > deadline:
            return False
        try:
            keys, _ = await replay_keys(sub, viewer)
        except Exception:  # noqa: BLE001
            keys = set()
        cache[marker] = key in keys
        return cache[marker]

    current = list(script)
    if not await fails(current):
        return None
    chunk = max(1, len(current) // 2)
    while chunk >= 1:
        i = 0
        shrunk = False
        while i < len(current):
            trial = current[:i] + current[i + chunk:]
            if trial and await fails(trial):
                current = trial
                shrunk = True
            else:
                i += chunk
            if time.time() > deadline:
                break
        if time.time() > deadline:
            break
        if not shrunk:
            chunk //= 2
    return current


# ---------------------------------------------------------------------------------------------- tests
async def run_seed(seed, viewer):
    started = time.time()
    async with fresh_env() as env:
        sim = Sim(env, seed, viewer=viewer)
        try:
            await sim.run_random(OPS)
        except Exception as exc:  # noqa: BLE001
            sim.fail(("driver-exception", type(exc).__name__), f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1200:]}")
    STATS["seeds"] += 1
    result = {"seed": seed, "viewer": viewer, "seconds": round(time.time() - started, 1), "fails": sim.fails,
              "ops": len(sim.script)}
    RESULTS.append(result)
    return result


@pytest.mark.parametrize("seed", range(SEEDS))
async def test_admin_model(seed):
    result = await run_seed(SEED_BASE + seed, viewer=False)
    assert not result["fails"], {str(k): v["message"][:300] for k, v in result["fails"].items()}


@pytest.mark.fit_types
@pytest.mark.parametrize("seed", range(max(1, SEEDS * 2 // 5)))
async def test_viewer_model(seed):
    result = await run_seed(SEED_BASE + 1000 + seed, viewer=True)
    assert not result["fails"], {str(k): v["message"][:300] for k, v in result["fails"].items()}


async def test_zz_summary():
    distinct = {}
    for result in RESULTS:
        for key, entry in result["fails"].items():
            slot = distinct.setdefault(key, {"seeds": [], "entry": entry, "viewer": result["viewer"], "count": 0})
            slot["seeds"].append(result["seed"])
            slot["count"] += entry["count"]
            if entry["op_no"] < slot["entry"]["op_no"]:
                slot["entry"] = entry
    report = {"stats": STATS, "seeds": [{k: v for k, v in r.items() if k != "fails"} | {"fail_keys": len(r["fails"])}
                                        for r in RESULTS], "distinct": []}
    deadline = time.time() + MINIMIZE_BUDGET_S
    lines = []
    for key, slot in sorted(distinct.items(), key=lambda kv: str(kv[0])):
        entry = slot["entry"]
        minimal = None
        if MINIMIZE and time.time() < deadline:
            script = [s for s in entry["script"]]
            minimal = await minimise(script, key, slot["viewer"], deadline)
        item = {"key": list(key), "message": entry["message"], "seeds": slot["seeds"][:10], "occurrences": slot["count"],
                "first_op_no": entry["op_no"], "tail_log": entry["log"][-12:],
                "minimal_script": minimal, "minimal_len": len(minimal) if minimal else None}
        if minimal:
            # readable log of the minimal reproduction
            try:
                _, log = await replay_keys(minimal, slot["viewer"])
                item["minimal_log"] = log
            except Exception as exc:  # noqa: BLE001
                item["minimal_log"] = [f"replay failed: {exc}"]
        report["distinct"].append(item)
        lines.append(f"- {key} seeds={slot['seeds'][:5]} n={slot['count']} :: {entry['message'][:300]}")
    report["outcomes"] = dict(sorted(OUTCOMES.items()))
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    print("FUZZ STATS", STATS)
    print("FUZZ OUTCOMES", json.dumps(report["outcomes"], indent=0))
    print("FUZZ REPORT written to", OUT)
    assert not distinct, f"{len(distinct)} distinct mismatches (report: {OUT}):\n" + "\n".join(lines)
