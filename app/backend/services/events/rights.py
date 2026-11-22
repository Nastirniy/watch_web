"""Who receives an event: an index over the same grants and the same tree as a request.

(node key, permission) -> the connected users holding a grant of that permission at that node. The grants come from
AuthContext at connect and from load_grants_of on every refresh, so the boundary of the user's type applies exactly as
on a request; the walk at delivery is AuthContext's own:

    p:<id>   the nodes in effect at the post (has_at_post): the root, every group above it not archived, the post
             itself when it is not archived
    g:<id>   the nodes not archived on one path with the group (on_path)
    root     the root only (has_at_root): system grants count there

Every grant enters the index, a grant at an archived node too: the walk leaves archived nodes out, so a tree change
needs no rebuild and a restore puts the grant back in effect at once. tests/test_event_rights.py compares this with
AuthContext over a table of cases.

An unknown post is a live node under the root (the ScopeTree rule): a post created since the last reload reaches the
holders at the root only, for at most events.rights_refresh_seconds.
"""
from typing import Dict, Iterator, Set, Tuple

from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree


class RightsIndex:
    def __init__(self, tree: ScopeTree = None):
        self.tree = tree if tree is not None else ScopeTree.empty()
        self.index: Dict[Tuple[str, str], Set[int]] = {}
        self.grants: Dict[int, Tuple[Grant, ...]] = {}

    def set_tree(self, tree: ScopeTree) -> None:
        self.tree = tree

    def set_user(self, user_id: int, grants: Tuple[Grant, ...]) -> None:
        """The grants of a connected user replace the ones stored for it"""
        self.drop_user(user_id)
        self.grants[user_id] = tuple(grants)
        for grant in grants:
            for permission in grant.permissions:
                self.index.setdefault((grant.node.key, permission), set()).add(user_id)

    def drop_user(self, user_id: int) -> None:
        for grant in self.grants.pop(user_id, ()):
            for permission in grant.permissions:
                entry = self.index.get((grant.node.key, permission))
                if entry is not None:
                    entry.discard(user_id)
                    if not entry:
                        del self.index[(grant.node.key, permission)]

    def _nodes(self, scope_key: str) -> Tuple[Node, ...]:
        node = Node.parse(scope_key)
        if node.kind == "post":
            return self.tree.nodes_in_effect_at_post(node.id)
        if node.kind == "group":
            return self.tree.nodes_on_path(node)
        return (ROOT,)

    def holders(self, scope_key: str, permission: str) -> Iterator[int]:
        """The users the permission is in effect for at the scope, each once"""
        seen: Set[int] = set()
        for node in self._nodes(scope_key):
            for user_id in self.index.get((node.key, permission), ()):
                if user_id not in seen:
                    seen.add(user_id)
                    yield user_id

    def allows(self, user_id: int, scope_key: str, permission: str) -> bool:
        """The permission is in effect for this user at the scope (a replay filters each row by it)"""
        return any(user_id in self.index.get((node.key, permission), ()) for node in self._nodes(scope_key))
