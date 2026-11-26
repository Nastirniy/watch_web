"""Who receives an event: an index over the same grants and the same tree as a request.

(node key, permission) -> the connected users holding a grant of that permission at that node. The grants come from
AuthContext at connect and from load_grants_of on every refresh, so the boundary of the user's type applies exactly as
on a request; the walk at delivery is AuthContext's own:

    p:<id>   the nodes in effect at the post (has_at_post): the root, every group above it not archived, the post
             itself when it is not archived
    g:<id>   the nodes not archived on one path with the group (on_path)
    root     the root only (has_at_root): system grants count there
    *        with a permission: a grant of it at any node not archived (has_anywhere, the require(all=[X]) of a route:
             roles and dictionaries); without one: every connected user (the user guides). publish.py HELD_ANYWHERE,
             EVERY_STREAM

Every grant enters the index, a grant at an archived node too: the walk leaves archived nodes out, so a tree change
needs no rebuild and a restore puts the grant back in effect at once. tests/events/test_event_rights.py compares this with
AuthContext over a table of cases.

An unknown post is a live node under the root (the ScopeTree rule). A create publishes scope.changed at the new node's
parent in its transaction, and the pump reloads the tree before it delivers anything of that tick: the events of a
new post reach the holders above it at once. Only a create made with events off is seen at the next rights refresh.

fingerprint() is what the hub compares to tell a tab its rights changed (rights_changed): equal fingerprints mean equal
rights in GET /auth/me (permissions, system_permissions, root_permissions, post_permissions, group_permissions), the
test in tests/events/test_event_rights.py. It is the permissions in effect per node and kind (a grant at an archived node puts
nothing in effect; which role gives a permission does not count) plus, for each group holding some, the posts and
groups below it (post_permissions and group_permissions list them). It may differ with equal rights in a rare case (a
post moved between two nested groups that hold the same permissions): one refetch for nothing, never a change missed.
"""
from typing import Dict, FrozenSet, Hashable, Iterator, Optional, Set, Tuple

from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree

ANYWHERE_KEY = "*"       # services/events/publish.py: never a node key
Reach = Tuple[FrozenSet[int], FrozenSet[int]]       # the posts and the groups below a group, archived ones included


class RightsIndex:
    def __init__(self, tree: ScopeTree = None):
        self.tree = tree if tree is not None else ScopeTree.empty()
        self.index: Dict[Tuple[str, str], Set[int]] = {}
        self.grants: Dict[int, Tuple[Grant, ...]] = {}
        # permission -> user -> the nodes the user holds it at (held anywhere: one of them not archived)
        self.anywhere: Dict[str, Dict[int, Set[Node]]] = {}
        # group id -> its Reach on the current tree, and on the tree before it: a reach that did not change keeps its
        # object, so comparing the fingerprints of every connected user after a tree change costs no walk per user
        self._reach: Dict[int, Reach] = {}
        self._reach_before: Dict[int, Reach] = {}

    def set_tree(self, tree: ScopeTree) -> None:
        if tree is not self.tree:
            self._reach_before = {**self._reach_before, **self._reach}
            self._reach = {}
        self.tree = tree

    def set_user(self, user_id: int, grants: Tuple[Grant, ...]) -> None:
        """The grants of a connected user replace the ones stored for it"""
        self.drop_user(user_id)
        self.grants[user_id] = tuple(grants)
        for grant in grants:
            for permission in grant.permissions:
                self.index.setdefault((grant.node.key, permission), set()).add(user_id)
                self.anywhere.setdefault(permission, {}).setdefault(user_id, set()).add(grant.node)

    def drop_user(self, user_id: int) -> None:
        for grant in self.grants.pop(user_id, ()):
            for permission in grant.permissions:
                entry = self.index.get((grant.node.key, permission))
                if entry is not None:
                    entry.discard(user_id)
                    if not entry:
                        del self.index[(grant.node.key, permission)]
                holders = self.anywhere.get(permission)
                if holders is not None:
                    holders.pop(user_id, None)
                    if not holders:
                        del self.anywhere[permission]

    def _nodes(self, scope_key: str) -> Tuple[Node, ...]:
        node = Node.parse(scope_key)
        if node.kind == "post":
            return self.tree.nodes_in_effect_at_post(node.id)
        if node.kind == "group":
            return self.tree.nodes_on_path(node)
        return (ROOT,)

    def _held_anywhere(self, nodes: Set[Node]) -> bool:
        return any(not self.tree.is_archived(node) for node in nodes)

    def holders(self, scope_key: str, permission: Optional[str]) -> Iterator[int]:
        """The users the permission is in effect for at the scope, each once. "*" with a permission: held at some node
        in effect; "*" without one: every user of the index (connected, whatever their grants)"""
        if scope_key == ANYWHERE_KEY:
            if permission is None:
                yield from list(self.grants)
            else:
                yield from [user_id for user_id, nodes in self.anywhere.get(permission, {}).items()
                            if self._held_anywhere(nodes)]
            return
        seen: Set[int] = set()
        for node in self._nodes(scope_key):
            for user_id in self.index.get((node.key, permission), ()):
                if user_id not in seen:
                    seen.add(user_id)
                    yield user_id

    def allows(self, user_id: int, scope_key: str, permission: Optional[str]) -> bool:
        """The permission is in effect for this user at the scope (a replay filters each row by it)"""
        if scope_key == ANYWHERE_KEY:
            if permission is None:
                return user_id in self.grants
            return self._held_anywhere(self.anywhere.get(permission, {}).get(user_id, set()))
        return any(user_id in self.index.get((node.key, permission), ()) for node in self._nodes(scope_key))

    def _reach_of(self, group_id: int) -> Reach:
        reach = self._reach.get(group_id)
        if reach is None:
            node = Node.group(group_id)
            reach = (self.tree.posts_under(node), self.tree.groups_under(node))
            before = self._reach_before.pop(group_id, None)
            reach = self._reach[group_id] = before if before == reach else reach
        return reach

    def fingerprint(self, user_id: int) -> Optional[Hashable]:
        """What the user's grants put in effect on the current tree, comparable with ==; None for a user not in the
        index"""
        grants = self.grants.get(user_id)
        if grants is None:
            return None
        held: Dict[Tuple[Node, str], FrozenSet[str]] = {}
        for grant in grants:
            if grant.permissions and not self.tree.is_archived(grant.node):
                key = (grant.node, grant.kind)
                before = held.get(key)
                held[key] = grant.permissions if before is None else before | grant.permissions
        reach = frozenset((node.id, self._reach_of(node.id)) for node, _ in held if node.kind == "group")
        return frozenset(held.items()), reach
