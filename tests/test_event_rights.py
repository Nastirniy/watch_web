"""The stream's answer equals AuthContext's for every kind of node. A table, not examples: a new rule breaks here."""
import pytest

from app.backend.auth.context import AuthContext, grants_of, load_grants, load_grants_of
from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree
from app.backend.enums.general import Permissions as P, UserType
from app.backend.models.user import User
from app.backend.services.events.rights import RightsIndex
from tests.helpers import give, make_group, make_post, make_user

#        root
#        +-- g:1
#        |   +-- g:2
#        |   |   +-- p:7
#        |   +-- p:9
#        +-- g:3
#        |   +-- p:8
#        +-- p:20
GROUP_PARENT = {1: None, 2: 1, 3: None}
POST_GROUP = {7: 2, 8: 3, 9: 1, 20: None}

CASES = [
    # grant node, event scope, permission held, delivered?
    ("root", "p:7", True, True),
    ("g:2", "p:7", True, True),                # above the post
    ("g:1", "p:7", True, True),                # two levels above
    ("g:2", "p:8", True, False),               # another group
    ("p:7", "p:7", True, True),
    ("p:7", "p:8", True, False),
    ("p:7", "g:2", True, True),                # group scope: on one path with the grant
    ("p:7", "g:1", True, True),
    ("g:1", "g:2", True, True),                # above the group
    ("g:2", "g:1", True, True),                # below the group
    ("g:3", "g:2", True, False),
    ("p:8", "g:2", True, False),
    ("root", "g:2", True, True),
    ("root", "root", True, True),
    ("p:7", "root", True, False),              # root scope: at the root only
    ("g:1", "root", True, False),
    ("root", "p:7", False, False),             # another permission
    ("g:2", "g:2", False, False),
    ("root", "p:99", True, True),              # a post the tree does not know yet: under the root
    ("g:1", "p:99", True, False),
    ("p:99", "p:99", True, True),
]


def tree(archived_groups=(), archived_posts=()) -> ScopeTree:
    return ScopeTree(group_parent=GROUP_PARENT, archived_groups=frozenset(archived_groups), post_group=POST_GROUP,
                     archived_posts=frozenset(archived_posts))


def grant(node_key: str, permission: str = P.POST_VIEW.value, kind: str = "tree") -> Grant:
    return Grant(1, "role", "Role", False, Node.parse(node_key), frozenset({permission}), assignment_id=1, kind=kind)


def context_answer(auth: AuthContext, scope_key: str, permission: str) -> bool:
    node = Node.parse(scope_key)
    if node.kind == "post":
        return auth.has_at_post(permission, node.id)
    if node.kind == "group":
        return auth.on_path(permission, node)
    return auth.has_at_root(permission)


def index_answer(index: RightsIndex, scope_key: str, permission: str) -> bool:
    return 5 in set(index.holders(scope_key, permission))


def both(scope_tree: ScopeTree, grants, scope_key: str, permission: str = P.POST_VIEW.value):
    auth = AuthContext(user=User(id=5, username="u", username_alias="U"), grants=tuple(grants), tree=scope_tree)
    index = RightsIndex(scope_tree)
    index.set_user(5, tuple(grants))
    return context_answer(auth, scope_key, permission), index_answer(index, scope_key, permission)


@pytest.mark.parametrize("grant_key, scope_key, held, delivered", CASES)
def test_the_index_agrees_with_auth_context(grant_key, scope_key, held, delivered):
    permission = P.POST_VIEW.value if held else P.POST_UPDATE.value
    expected, actual = both(tree(), [grant(grant_key, permission)], scope_key)
    assert expected == delivered, "the table itself is wrong"
    assert actual == expected


ARCHIVE_CASES = [
    # archived groups, archived posts, grant node, scope
    ((), (7,), "p:7", "p:7"),          # an archived post stops its own grant
    ((), (7,), "g:2", "p:7"),          # and keeps the inherited ones
    ((), (7,), "root", "p:7"),
    ((2,), (), "g:2", "p:7"),          # an archived group is skipped
    ((2,), (), "g:1", "p:7"),          # its parents still apply
    ((2,), (), "p:7", "p:7"),
    ((2,), (), "g:2", "g:2"),
    ((2,), (), "g:1", "g:2"),
    ((2,), (), "p:7", "g:1"),
    ((2,), (), "g:2", "g:1"),          # a grant at an archived group counts nowhere
    ((1,), (), "p:9", "g:1"),
    ((1,), (7,), "p:7", "g:1"),
]


@pytest.mark.parametrize("archived_groups, archived_posts, grant_key, scope_key", ARCHIVE_CASES)
def test_archived_nodes_agree(archived_groups, archived_posts, grant_key, scope_key):
    expected, actual = both(tree(archived_groups, archived_posts), [grant(grant_key)], scope_key)
    assert actual == expected


def test_a_tree_change_is_re_evaluated_from_the_stored_grants():
    index = RightsIndex(tree())
    index.set_user(5, (grant("p:7"),))
    assert list(index.holders("p:7", P.POST_VIEW.value)) == [5]
    index.set_tree(tree(archived_posts=(7,)))
    assert list(index.holders("p:7", P.POST_VIEW.value)) == []
    index.set_tree(tree())
    assert list(index.holders("p:7", P.POST_VIEW.value)) == [5]


def test_set_user_replaces_and_drop_user_removes():
    index = RightsIndex(tree())
    index.set_user(5, (grant("g:1"),))
    index.set_user(5, (grant("g:3"),))
    assert list(index.holders("p:7", P.POST_VIEW.value)) == [] and list(index.holders("p:8", P.POST_VIEW.value)) == [5]
    index.drop_user(5)
    assert list(index.holders("p:8", P.POST_VIEW.value)) == [] and index.grants == {}
    assert index.index == {}, "no empty entries left behind"


def test_a_user_is_named_once_whatever_the_number_of_grants():
    index = RightsIndex(tree())
    index.set_user(5, (grant("root"), grant("g:1"), grant("p:7")))
    index.set_user(6, (grant("g:2"),))
    assert sorted(index.holders("p:7", P.POST_VIEW.value)) == [5, 6]


def test_a_system_grant_counts_at_the_root():
    expected, actual = both(tree(), [grant("root", P.AUDIT_VIEW.value, kind="system")], "root", P.AUDIT_VIEW.value)
    assert expected and actual


def test_a_permission_outside_the_type_never_reaches_the_index():
    viewer = User(id=5, username="v", username_alias="V", user_type=UserType.VIEWER.value)
    rows = [(1, None, None, 10, "custom", "Custom", None, P.POST_UPDATE.value, "tree"),
            (1, None, None, 10, "custom", "Custom", None, P.POST_VIEW.value, "tree")]
    grants = grants_of(rows, user=viewer)
    index = RightsIndex(tree())
    index.set_user(5, grants)
    assert list(index.holders("p:7", P.POST_VIEW.value)) == [5]
    assert list(index.holders("p:7", P.POST_UPDATE.value)) == []


async def test_load_grants_of_is_load_grants_for_each_user(db):
    group = await make_group(db)
    post = await make_post(db, group_id=group.id)
    first = await make_user(db, [P.POST_VIEW, P.POST_UPDATE], post_id=post.id)
    second = await make_user(db, user_type=UserType.VIEWER)
    await give(db, second, [P.POST_VIEW, P.POST_UPDATE], Node.group(group.id))
    third = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, third, [P.AUDIT_VIEW, P.POST_VIEW])
    loaded = await load_grants_of(db, [first, second, third])
    for user in (first, second, third):
        assert loaded[user.id] == await load_grants(db, user)
    assert P.POST_UPDATE.value not in set().union(*(g.permissions for g in loaded[second.id])), "the viewer boundary"
    assert await load_grants_of(db, []) == {}
