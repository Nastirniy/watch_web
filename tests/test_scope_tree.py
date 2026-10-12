"""Pure scope evaluator (auth/scope.py): nodes, the tree, inheritance and archived nodes. No database."""
from dataclasses import replace

import pytest

from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree

# East(1) > Kharkiv(2) > posts 7, 8; East > post 12; West(3) > post 9; post 20 at /
TREE = ScopeTree(
    group_parent={1: None, 2: 1, 3: None},
    archived_groups=frozenset(),
    post_group={7: 2, 8: 2, 12: 1, 9: 3, 20: None},
    archived_posts=frozenset(),
)


def test_group_grant_reaches_descendant_posts():
    assert TREE.posts_under(Node.group(1)) == {7, 8, 12}
    assert TREE.posts_under(Node.group(2)) == {7, 8}
    assert TREE.posts_under(ROOT) == {7, 8, 9, 12, 20}
    assert TREE.posts_under(Node.post(9)) == {9}


def test_post_added_later_is_covered():
    later = replace(TREE, post_group={**TREE.post_group, 13: 2})
    assert 13 in later.posts_under(Node.group(1))
    assert 13 in later.posts_under(Node.group(2))


def test_ancestors_start_with_the_node_and_end_at_root():
    assert TREE.ancestors(Node.post(7)) == (Node.post(7), Node.group(2), Node.group(1), ROOT)
    assert TREE.ancestors(Node.group(2)) == (Node.group(2), Node.group(1), ROOT)
    assert TREE.ancestors(Node.post(20)) == (Node.post(20), ROOT)
    assert TREE.ancestors(ROOT) == (ROOT,)


def test_nodes_in_effect_at_post():
    assert TREE.nodes_in_effect_at_post(7) == (ROOT, Node.group(1), Node.group(2), Node.post(7))
    assert TREE.nodes_in_effect_at_post(20) == (ROOT, Node.post(20))


def test_archived_post_direct_grant_stops_inherited_stays():
    t = replace(TREE, archived_posts=frozenset({7}))
    assert Node.post(7) not in t.nodes_in_effect_at_post(7) and Node.group(1) in t.nodes_in_effect_at_post(7)
    assert ROOT in t.nodes_in_effect_at_post(7)
    # posts_under is structural: archived posts are included
    assert 7 in t.posts_under(Node.group(2)) and 7 in t.posts_under(ROOT)


def test_archived_group_grant_does_not_apply():
    t = replace(TREE, archived_groups=frozenset({2}))
    in_effect = t.nodes_in_effect_at_post(8)
    assert Node.group(2) not in in_effect
    # the walk goes on above the archived group
    assert Node.group(1) in in_effect and ROOT in in_effect and Node.post(8) in in_effect


def test_post_in_archived_or_unknown_group():
    # a post restored while its group is archived keeps group_id
    t = replace(TREE, archived_groups=frozenset({3}))
    assert t.nodes_in_effect_at_post(9) == (ROOT, Node.post(9))
    assert t.posts_under(Node.group(3)) == {9}
    # a group missing from the map: a live group directly under the root, no crash
    stray = replace(TREE, post_group={**TREE.post_group, 30: 99})
    assert stray.ancestors(Node.post(30)) == (Node.post(30), Node.group(99), ROOT)
    assert stray.nodes_in_effect_at_post(30) == (ROOT, Node.group(99), Node.post(30))
    assert 30 in stray.posts_under(ROOT) and stray.posts_under(Node.group(99)) == {30}
    # a post missing from the map: a live post directly under the root
    assert TREE.nodes_in_effect_at_post(404) == (ROOT, Node.post(404))
    # an id no post can have (a query parameter nobody validated): the root only, never an error
    assert TREE.nodes_in_effect_at_post(0) == (ROOT,) and TREE.nodes_in_effect_at_post(-3) == (ROOT,)


def test_cyclic_parents_do_not_loop():
    cyclic = replace(TREE, group_parent={1: 2, 2: 1, 3: None})
    assert cyclic.ancestors(Node.post(7))[-1] == ROOT
    assert set(cyclic.ancestors(Node.post(7))) == {Node.post(7), Node.group(2), Node.group(1), ROOT}


def test_contains():
    assert TREE.contains(Node.group(1), Node.post(8)) and not TREE.contains(Node.group(1), Node.post(9))
    assert TREE.contains(ROOT, Node.group(3)) and not TREE.contains(Node.group(1), ROOT)
    assert TREE.contains(Node.post(8), Node.post(8)) and not TREE.contains(Node.post(8), Node.post(7))
    assert TREE.contains(Node.group(1), Node.group(2)) and not TREE.contains(Node.group(2), Node.group(1))


def test_is_archived():
    t = replace(TREE, archived_groups=frozenset({2}), archived_posts=frozenset({9}))
    assert t.is_archived(Node.group(2)) and t.is_archived(Node.post(9))
    assert not t.is_archived(ROOT) and not t.is_archived(Node.group(1)) and not t.is_archived(Node.post(7))


def test_node_key_roundtrip():
    assert Node.parse("g:3") == Node.group(3) and Node.post(7).key == "p:7" and Node.parse("root") == ROOT
    assert ROOT.key == "root" and Node.root() == ROOT
    for node in (ROOT, Node.group(3), Node.post(7)):
        assert Node.parse(node.key) == node


@pytest.mark.parametrize("key", ["", "x:1", "g:", "p:abc", "root:1", "g:0", "p:-1", "G:3", " p:7", "p:007", "Root"])
def test_node_parse_refuses_malformed_keys(key):
    with pytest.raises(ValueError):
        Node.parse(key)


def test_node_refuses_inconsistent_ids():
    with pytest.raises(ValueError):
        Node("root", 1)
    with pytest.raises(ValueError):
        Node("group")
    with pytest.raises(ValueError):
        Node("post", 0)


def test_grant_is_a_value():
    grant = Grant(role_id=1, role_name="ops", role_display_name="Ops", role_is_builtin=False,
                  node=Node.post(7), permissions=frozenset({"observation.view"}))
    assert grant.assignment_id is None
    assert grant == replace(grant)


def test_empty_tree():
    empty = ScopeTree.empty()
    assert empty.posts_under(ROOT) == frozenset()
    assert empty.nodes_in_effect_at_post(5) == (ROOT, Node.post(5))
