"""User guides (part 2, task 7): the audience of a guide (decision 8).

A guide with an audience node is visible to a caller when the caller holds guide.update (a manager: every guide),
when the node lies on a path through the caller's home post (either direction), or when one of the caller's tree
assignments in effect contains the node or lies inside it. An archived audience node hides the guide from everyone
but managers. A guide the caller may not see is left out of the list and answers 404 guide_not_found everywhere.

The tree of the route tests: corps C > brigades B1, B2; posts P1 and P1b in B1, P2 in B2; guide G for B1.
"""
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from app.backend.api.v1 import V1
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree
from app.backend.config import settings
from app.backend.enums.general import BuiltinRole, Permissions, UserType
from app.backend.enums.routes import GuideRoutes
from app.backend.models.post import Post
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from app.backend.services.guides import guide_visible, related_keys
from tests.helpers import builtin_role, give, make_admin, make_group, make_post, make_role

GUIDES = V1.path(GuideRoutes.ROOT)
PDF = (Path(__file__).resolve().parent / "fixtures" / "files" / "guide.pdf").read_bytes()


def by_id(guide_id: int) -> str:
    return V1.path(GuideRoutes.GUIDE_BY_ID, guide_id=guide_id)


def download_path(guide_id: int, file_id: int) -> str:
    return V1.path(GuideRoutes.FILE_DOWNLOAD, guide_id=guide_id, file_id=file_id)


# ===================================================================================================== pure rule

# groups: C=1 (corps), B1=2 and B2=3 (brigades under C), A=4 (archived brigade under C)
# posts: P1=10 and P1b=11 in B1, P2=20 in B2, PA=30 in A, PX=40 archived in B1
C, B1, B2, A = (Node.group(n) for n in (1, 2, 3, 4))
P1, P1B, P2, PA, PX = (Node.post(n) for n in (10, 11, 20, 30, 40))
UNKNOWN = Node.group(99)
TREE = ScopeTree(
    group_parent={1: None, 2: 1, 3: 1, 4: 1},
    archived_groups=frozenset({4}),
    post_group={10: 2, 11: 2, 20: 3, 30: 4, 40: 2},
    archived_posts=frozenset({40}),
    group_kind={1: "corps", 2: "brigade", 3: "brigade", 4: "brigade"},
)
EVERY_NODE = (C, B1, B2, A, P1, P1B, P2, PA, PX)
LIVE = (C, B1, B2, P1, P1B, P2, PA)     # PA is live: only the audience node itself counts


def _grant(node: Node, *permissions: str, kind: str = "tree") -> Grant:
    return Grant(role_id=1, role_name="role", role_display_name="Role", role_is_builtin=False, node=node,
                 permissions=frozenset(permissions), kind=kind)


def _auth(home: Optional[Node] = None, *grants: Grant, tree: ScopeTree = TREE) -> AuthContext:
    user = User(id=1, username="someone", username_alias="Someone", user_type=UserType.VIEWER.value,
                post_id=home.id if home is not None else None)
    return AuthContext(user=user, grants=tuple(grants), tree=tree)


MANAGER = _auth(None, _grant(ROOT, Permissions.GUIDE_UPDATE.value, kind="system"))


@dataclass(frozen=True)
class Case:
    name: str
    auth: AuthContext
    sees: frozenset           # the audience nodes of EVERY_NODE the caller sees


CASES = [
    Case("no home, no grants", _auth(), frozenset()),
    Case("home P1: its brigade and corps, itself", _auth(P1), frozenset({P1, B1, C})),
    Case("home P2: not the other brigade", _auth(P2), frozenset({P2, B2, C})),
    Case("home P1b: not the sibling post P1", _auth(P1B), frozenset({P1B, B1, C})),
    Case("home archived PX: the home counts, the archived node itself is hidden", _auth(PX), frozenset({B1, C})),
    Case("home PA under the archived brigade A: A is hidden", _auth(PA), frozenset({PA, C})),
    Case("tree grant at C: everything live below it", _auth(None, _grant(C, "observation.view")),
         frozenset(LIVE)),
    Case("tree grant at B1: its posts and the corps above", _auth(None, _grant(B1, "observation.view")),
         frozenset({C, B1, P1, P1B})),
    Case("tree grant at P1: the post and the units above", _auth(None, _grant(P1, "observation.view")),
         frozenset({P1, B1, C})),
    Case("tree grant at the root: every live node", _auth(None, _grant(ROOT, "observation.view")),
         frozenset(LIVE)),
    Case("a tree grant without permissions is still an assignment", _auth(None, _grant(B2)),
         frozenset({C, B2, P2})),
    Case("home P2 and a grant at P1: the union", _auth(P2, _grant(P1, "observation.view")),
         frozenset({P1, B1, C, P2, B2})),
    Case("tree grant at the archived A: applies nowhere", _auth(None, _grant(A, "observation.view")), frozenset()),
    Case("tree grant at the archived PX: applies nowhere", _auth(None, _grant(PX, "observation.view")),
         frozenset()),
    Case("a system grant never widens", _auth(None, _grant(ROOT, Permissions.AUDIT_VIEW.value, kind="system")),
         frozenset()),
    Case("guide.archive alone is no manager", _auth(None, _grant(ROOT, Permissions.GUIDE_ARCHIVE.value,
                                                                 kind="system")), frozenset()),
    Case("manager: every node, archived ones too", MANAGER, frozenset(EVERY_NODE)),
]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_guide_visible_follows_decision_8(case):
    assert frozenset(node for node in EVERY_NODE if guide_visible(case.auth, node)) == case.sees


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_guide_visible_without_an_audience_is_true_for_all(case):
    assert guide_visible(case.auth, None)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_related_keys_are_what_guide_visible_sees(case):
    """The list (SQL: audience_key IN related keys) and get-by-id (guide_visible) apply one rule"""
    keys = related_keys(case.auth)
    if case.auth is MANAGER:
        assert keys is None
        return
    assert keys == frozenset(node.key for node in EVERY_NODE if guide_visible(case.auth, node))
    assert ROOT.key not in keys


def test_an_unknown_audience_node_is_seen_only_by_managers():
    """A node the tree does not know (no such row): related_keys cannot list it, so guide_visible does not see it
    either, not even through a tree grant at the root"""
    for auth in (_auth(P1), _auth(None, _grant(ROOT, "observation.view"))):
        assert not guide_visible(auth, UNKNOWN)
        assert UNKNOWN.key not in related_keys(auth)
    assert guide_visible(MANAGER, UNKNOWN)


def test_an_archived_audience_on_the_own_path_is_hidden():
    """B1 archived: a user homed at P1 (B1 is on the path) and an assignment at C (contains B1) both lose G"""
    tree = ScopeTree(group_parent=TREE.group_parent, archived_groups=frozenset({2, 4}), post_group=TREE.post_group,
                     archived_posts=TREE.archived_posts, group_kind=TREE.group_kind)
    for auth in (_auth(P1, tree=tree), _auth(None, _grant(C, "observation.view"), tree=tree),
                 _auth(None, _grant(P1, "observation.view"), tree=tree)):
        assert not guide_visible(auth, B1)
        assert B1.key not in related_keys(auth)
        assert guide_visible(auth, C) and guide_visible(auth, P1)
    manager = _auth(None, _grant(ROOT, Permissions.GUIDE_UPDATE.value, kind="system"), tree=tree)
    assert guide_visible(manager, B1)


def test_the_rule_is_symmetric_on_one_path():
    """For every pair of live nodes: an assignment at one sees a guide for the other exactly when one contains the
    other (sibling subtrees never see each other)"""
    for anchor, audience in itertools.product(LIVE, LIVE):
        expected = TREE.contains(anchor, audience) or TREE.contains(audience, anchor)
        assert guide_visible(_auth(None, _grant(anchor, "observation.view")), audience) is expected, (anchor, audience)


# ===================================================================================================== routes

@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path / "media"))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "spool"))
    monkeypatch.setattr(settings.storage, "enable_nginx", False)
    (tmp_path / "media").mkdir()
    return tmp_path


@dataclass
class World:
    corps: ScopeGroup
    b1: ScopeGroup
    b2: ScopeGroup
    p1: Post
    p1b: Post
    p2: Post
    admin: User


async def _world(db) -> World:
    corps = await make_group(db, name="Corps C", kind="corps")
    b1 = await make_group(db, name="Brigade B1", parent_id=corps.id, kind="brigade")
    b2 = await make_group(db, name="Brigade B2", parent_id=corps.id, kind="brigade")
    p1 = await make_post(db, name="Post P1", group_id=b1.id)
    p1b = await make_post(db, name="Post P1b", group_id=b1.id)
    p2 = await make_post(db, name="Post P2", group_id=b2.id)
    return World(corps, b1, b2, p1, p1b, p2, await make_admin(db))


async def _member(db, post: Optional[Post] = None, name: Optional[str] = None) -> User:
    """A user homed at the post (none: no home), without any assignment"""
    user = User(username=name or f"member_{post.id if post else 'none'}", username_alias="Member",
                user_type=UserType.VIEWER.value, post_id=post.id if post else None)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _administrator_at(db, node: Node, name: str) -> User:
    """A user of type administrator with the built-in administrator role at the node, no home"""
    user = User(username=name, username_alias=name, user_type=UserType.ADMINISTRATOR.value)
    db.add(user)
    await db.flush()
    db.add(RoleAssignment(user_id=user.id, role_id=(await builtin_role(db, BuiltinRole.ADMINISTRATOR)).id,
                          **scope_columns(node)))
    await db.commit()
    await db.refresh(user)
    return user


async def _guide(client, headers, admin, audience: Optional[dict], title: str = "Guide") -> dict:
    body = {"title": title} if audience is None else {"title": title, "audience": audience}
    response = await client.post(GUIDES, headers=headers(admin), json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _with_file(client, headers, admin, guide: dict) -> int:
    response = await client.post(V1.path(GuideRoutes.FILES, guide_id=guide["id"]), headers=headers(admin),
                                 files={"file": ("guide.pdf", PDF, "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _listed(client, headers, user) -> list:
    response = await client.get(GUIDES, headers=headers(user))
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()]


async def _sees(client, headers, user, guide: dict, file_id: int) -> bool:
    """Whether the user sees the guide on the list, GET by id and the download; all three agree, and an invisible
    guide answers exactly what an unknown one does"""
    listed = guide["id"] in await _listed(client, headers, user)
    one = await client.get(by_id(guide["id"]), headers=headers(user))
    download = await client.get(download_path(guide["id"], file_id), headers=headers(user))
    if one.status_code == 200:
        assert listed and download.status_code == 200 and download.content == PDF, (one.text, download.text)
        assert [item["id"] for item in one.json()["files"]] == [file_id]
        return True
    for response in (one, download):
        assert response.status_code == 404, response.text
        problem = response.json()
        assert (problem["code"], problem["guide_id"]) == ("guide_not_found", guide["id"])
        assert "audience" not in response.text and "Brigade" not in response.text
    assert not listed
    return False


async def test_a_guide_for_b1_is_seen_along_its_path(client, db, headers, storage):
    w = await _world(db)
    guide = await _guide(client, headers, w.admin, {"kind": "group", "id": w.b1.id})
    file_id = await _with_file(client, headers, w.admin, guide)

    at_p2 = await _member(db, w.p2, "member_p2")
    operator_p2 = await _member(db, None, "operator_p2")
    await give(db, operator_p2, ["observation.view"], node=Node.post(w.p2.id))
    viewer_at_c = await _member(db, None, "viewer_at_c")
    await give(db, viewer_at_c, ["observation.view"], node=Node.group(w.corps.id))

    assert await _sees(client, headers, await _member(db, w.p1, "member_p1"), guide, file_id), "home P1 sees G"
    assert await _sees(client, headers, await _member(db, w.p1b, "member_p1b"), guide, file_id)
    assert not await _sees(client, headers, at_p2, guide, file_id), "home P2 does not"
    assert not await _sees(client, headers, operator_p2, guide, file_id), "nor an assignment at P2"
    assert await _sees(client, headers, await _administrator_at(db, Node.group(w.corps.id), "admin_c"), guide,
                       file_id), "an administrator assigned at C (contains B1)"
    assert await _sees(client, headers, viewer_at_c, guide, file_id), "an assignment at C without one in B1"
    assert await _sees(client, headers, await _administrator_at(db, Node.post(w.p1.id), "admin_p1"), guide,
                       file_id), "an assignment inside B1"
    assert not await _sees(client, headers, await _administrator_at(db, Node.group(w.b2.id), "admin_b2"), guide,
                           file_id), "the sibling brigade's administrator"
    assert await _sees(client, headers, w.admin, guide, file_id), "a manager"


async def test_a_guide_without_an_audience_is_seen_by_all(client, db, headers, storage):
    w = await _world(db)
    guide = await _guide(client, headers, w.admin, None)
    file_id = await _with_file(client, headers, w.admin, guide)
    for user in (await _member(db, w.p1, "a"), await _member(db, w.p2, "b"), await _member(db, None, "c"),
                 await _administrator_at(db, Node.group(w.b2.id), "d"), w.admin):
        assert await _sees(client, headers, user, guide, file_id), user.username


async def test_no_home_and_no_grants_sees_only_guides_without_an_audience(client, db, headers, storage):
    w = await _world(db)
    open_guide = await _guide(client, headers, w.admin, None, title="Open")
    for audience in ({"kind": "group", "id": w.corps.id}, {"kind": "group", "id": w.b1.id},
                     {"kind": "post", "id": w.p2.id}):
        await _guide(client, headers, w.admin, audience)
    nobody = await _member(db, None, "nobody")
    assert await _listed(client, headers, nobody) == [open_guide["id"]]

    auditor = await _member(db, None, "auditor")    # a system grant never widens the audience
    auditor.user_type = UserType.SYSTEM_ADMINISTRATOR.value
    db.add(auditor)
    await db.commit()
    db.add(SystemRoleAssignment(user_id=auditor.id, role_id=(await make_role(db, [Permissions.AUDIT_VIEW])).id))
    await db.commit()
    assert await _listed(client, headers, auditor) == [open_guide["id"]]


async def test_a_guide_for_a_post_is_not_seen_from_the_sibling_post(client, db, headers, storage):
    """Sibling posts are not on one path: a guide for P1 is not for the users of P1b, though both are in B1"""
    w = await _world(db)
    guide = await _guide(client, headers, w.admin, {"kind": "post", "id": w.p1.id})
    file_id = await _with_file(client, headers, w.admin, guide)
    assert await _sees(client, headers, await _member(db, w.p1, "at_p1"), guide, file_id)
    assert not await _sees(client, headers, await _member(db, w.p1b, "at_p1b"), guide, file_id)
    assert await _sees(client, headers, await _administrator_at(db, Node.group(w.b1.id), "admin_b1"), guide,
                       file_id)


async def test_an_archived_audience_node_hides_the_guide_from_all_but_managers(client, db, headers, storage):
    w = await _world(db)
    guide = await _guide(client, headers, w.admin, {"kind": "group", "id": w.b1.id})
    file_id = await _with_file(client, headers, w.admin, guide)
    at_p1 = await _member(db, w.p1, "at_p1")
    admin_c = await _administrator_at(db, Node.group(w.corps.id), "admin_c")
    assert await _sees(client, headers, at_p1, guide, file_id)

    w.b1.is_deleted = True
    db.add(w.b1)
    await db.commit()
    assert not await _sees(client, headers, at_p1, guide, file_id), "B1 is on the user's own path"
    assert not await _sees(client, headers, admin_c, guide, file_id), "C contains B1"
    assert await _sees(client, headers, w.admin, guide, file_id), "a manager still sees it"
    assert (await client.get(by_id(guide["id"]), headers=headers(w.admin))).json()["audience"]["id"] == w.b1.id


async def test_file_writes_on_an_invisible_guide_are_404(client, db, headers, storage):
    """guide.archive without guide.update is no manager: archive and restore of the guide and of its files answer
    404 guide_not_found for a guide it does not see, as an unknown one would"""
    w = await _world(db)
    guide = await _guide(client, headers, w.admin, {"kind": "group", "id": w.b1.id})
    file_id = await _with_file(client, headers, w.admin, guide)
    archiver = User(username="archiver", username_alias="Archiver", user_type=UserType.SYSTEM_ADMINISTRATOR.value,
                    post_id=w.p2.id)
    db.add(archiver)
    await db.commit()
    db.add(SystemRoleAssignment(user_id=archiver.id, role_id=(await make_role(db, [Permissions.GUIDE_ARCHIVE])).id))
    await db.commit()

    for path in (V1.path(GuideRoutes.FILE_ARCHIVE, guide_id=guide["id"], file_id=file_id),
                 V1.path(GuideRoutes.FILE_RESTORE, guide_id=guide["id"], file_id=file_id),
                 V1.path(GuideRoutes.GUIDE_ARCHIVE, guide_id=guide["id"]),
                 V1.path(GuideRoutes.GUIDE_RESTORE, guide_id=guide["id"])):
        response = await client.post(path, headers=headers(archiver))
        assert response.status_code == 404, (path, response.text)
        assert (response.json()["code"], response.json()["guide_id"]) == ("guide_not_found", guide["id"])
    archived = await client.get(by_id(guide["id"]), headers=headers(archiver), params={"include_deleted": True})
    assert archived.status_code == 404, archived.text

    # on its own path (home P1) the same user archives the file
    archiver.post_id = w.p1.id
    db.add(archiver)
    await db.commit()
    response = await client.post(V1.path(GuideRoutes.FILE_ARCHIVE, guide_id=guide["id"], file_id=file_id),
                                 headers=headers(archiver))
    assert response.status_code == 200, response.text


async def test_writes_by_a_non_manager_are_403_before_the_lookup(client, db, headers, storage):
    w = await _world(db)
    guide = await _guide(client, headers, w.admin, {"kind": "group", "id": w.b1.id})
    at_p2 = await _member(db, w.p2, "at_p2")
    response = await client.patch(by_id(guide["id"]), headers=headers(at_p2), json={"title": "x"})
    assert response.status_code == 403 and response.json()["missing"] == ["guide.update"], response.text
    upload = await client.post(V1.path(GuideRoutes.FILES, guide_id=guide["id"]), headers=headers(at_p2),
                               files={"file": ("guide.pdf", PDF, "application/pdf")})
    assert upload.status_code == 403, upload.text


async def test_the_list_filters_in_sql_and_pages_after_it(client, db, headers, storage):
    """Hidden guides are left out before the page is cut: a page holds only guides the caller sees"""
    w = await _world(db)
    seen = []
    for n in range(4):
        hidden = await _guide(client, headers, w.admin, {"kind": "group", "id": w.b2.id}, title=f"Hidden {n}")
        assert hidden["id"]
        seen.append((await _guide(client, headers, w.admin, {"kind": "post", "id": w.p1.id}, title=f"Seen {n}"))["id"])
    at_p1 = await _member(db, w.p1, "at_p1")
    page = await client.get(GUIDES, headers=headers(at_p1), params={"page": 1, "page_size": 3, "sort_by": "title"})
    assert [item["id"] for item in page.json()] == seen[:3]
    page = await client.get(GUIDES, headers=headers(at_p1), params={"page": 2, "page_size": 3, "sort_by": "title"})
    assert [item["id"] for item in page.json()] == seen[3:]
