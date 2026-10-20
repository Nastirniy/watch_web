"""The scope tree and its evaluation: pure, no database, no FastAPI.

    /                    ROOT
    +-- group 1          Node.group(1)
    |   +-- group 2
    |   |   +-- post 7   Node.post(7)
    |   +-- post 12
    +-- post 20

A role assignment (Grant) holds its permissions at one node; they apply to the node and everything below it,
posts added later included. Effective permissions at a post come from the root, every non-archived ancestor group
(an archived group is skipped, the walk goes on to its parents) and the post itself when it is not archived:
a direct grant on an archived post stops applying, inherited ones keep read access to its data.

ScopeTree is built by auth/context.py (load_scope_tree) from every group and every post, archived ones included.
Nodes missing from the maps (inconsistent data, a post created after the tree was read) never fail: a group or a
post the maps do not know is a live node directly under the root.
"""
from dataclasses import dataclass, field
from functools import cached_property
from typing import Dict, FrozenSet, List, Literal, Mapping, Optional, Tuple

NodeKind = Literal["root", "group", "post"]

_PREFIXES = {"group": "g", "post": "p"}
_KINDS = {prefix: kind for kind, prefix in _PREFIXES.items()}
ROOT_KEY = "root"
PLAIN_GROUP = "group"   # ScopeGroupKind.GROUP; every other kind is a unit (corps, brigade)


@dataclass(frozen=True)
class Node:
    """A node of the scope tree. Its key is the scope_key of a role assignment: "root", "g:<id>", "p:<id>"."""
    kind: NodeKind
    id: Optional[int] = None

    def __post_init__(self):
        if self.kind == "root":
            if self.id is not None:
                raise ValueError("the root node has no id")
        elif self.kind in _PREFIXES:
            if not isinstance(self.id, int) or isinstance(self.id, bool) or self.id < 1:
                raise ValueError(f"a {self.kind} node needs a positive integer id")
        else:
            raise ValueError(f"unknown node kind {self.kind!r}")

    @classmethod
    def root(cls) -> "Node":
        return cls("root")

    @classmethod
    def group(cls, group_id: int) -> "Node":
        return cls("group", group_id)

    @classmethod
    def post(cls, post_id: int) -> "Node":
        return cls("post", post_id)

    @property
    def key(self) -> str:
        return ROOT_KEY if self.kind == "root" else f"{_PREFIXES[self.kind]}:{self.id}"

    @classmethod
    def parse(cls, key: str) -> "Node":
        """The node of a key; ValueError for anything that is not exactly "root", "g:<id>" or "p:<id>" """
        if key == ROOT_KEY:
            return ROOT
        prefix, separator, digits = key.partition(":")
        if not separator or prefix not in _KINDS or not digits.isascii() or not digits.isdigit():
            raise ValueError(f"not a scope key: {key!r}")
        node = cls(_KINDS[prefix], int(digits))
        if node.key != key:   # one key per node: no leading zeros
            raise ValueError(f"not a canonical scope key: {key!r}")
        return node


ROOT = Node.root()


@dataclass(frozen=True)
class Grant:
    """Permissions a user holds at one node through one role (a role assignment, or its transitional equivalent)"""
    role_id: int
    role_name: str
    role_display_name: str
    role_is_builtin: bool
    node: Node
    permissions: FrozenSet[str]
    assignment_id: Optional[int] = None


@dataclass(frozen=True)
class ScopeTree:
    """Every group (parent, archived flag, kind) and every post (group, archived flag). None as a parent or a group
    means directly under the root. A group missing from group_kind is a plain group. The maps are read, never
    changed."""
    group_parent: Mapping[int, Optional[int]]
    archived_groups: FrozenSet[int]
    post_group: Mapping[int, Optional[int]]
    archived_posts: FrozenSet[int]
    group_kind: Mapping[int, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ScopeTree":
        return cls(group_parent={}, archived_groups=frozenset(), post_group={}, archived_posts=frozenset())

    # ------------------------------------------------------------------ structure
    def _group_chain(self, group_id: Optional[int]) -> Tuple[int, ...]:
        """The group and its ancestors, nearest first. Stops at a group seen twice (a cycle is bad data)."""
        chain: List[int] = []
        while group_id is not None and group_id not in chain:
            chain.append(group_id)
            group_id = self.group_parent.get(group_id)
        return tuple(chain)

    @cached_property
    def _post_chains(self) -> Dict[int, Tuple[int, ...]]:
        """post id -> the groups above it, nearest first"""
        return {post_id: self._group_chain(group_id) for post_id, group_id in self.post_group.items()}

    def _groups_above(self, node: Node) -> Tuple[int, ...]:
        if node.kind == "post":
            return self._post_chains.get(node.id, ())
        if node.kind == "group":
            return self._group_chain(node.id)[1:]
        return ()

    def ancestors(self, node: Node) -> Tuple[Node, ...]:
        """The node first, then every group above it (archived ones included), ROOT last"""
        if node.kind == "root":
            return (ROOT,)
        groups = tuple(Node.group(group_id) for group_id in self._groups_above(node))
        return (node, *groups, ROOT)

    def contains(self, outer: Node, inner: Node) -> bool:
        """inner is outer or lies below it"""
        return outer in self.ancestors(inner)

    def posts_under(self, node: Node) -> FrozenSet[int]:
        """Every post at or below the node, archived posts included"""
        if node.kind == "root":
            return frozenset(self.post_group)
        if node.kind == "post":
            return frozenset({node.id})
        return frozenset(post_id for post_id, chain in self._post_chains.items() if node.id in chain)

    # ------------------------------------------------------------------ units
    def _units(self, chain: Tuple[int, ...]) -> Dict[str, int]:
        """unit kind -> group id for every group of the chain that is a unit, top down (archived ones included)"""
        units: Dict[str, int] = {}
        for group_id in reversed(chain):
            kind = self.group_kind.get(group_id, PLAIN_GROUP)
            if kind != PLAIN_GROUP:
                units[kind] = group_id
        return units

    def units_of_post(self, post_id: int) -> Dict[str, int]:
        """The units the post sits in now: {"corps": 1, "brigade": 2}, {} outside any unit or for an unknown post"""
        return self._units(self._post_chains.get(post_id, ()))

    def units_of_group(self, group_id: int) -> Dict[str, int]:
        """The group itself when it is a unit and the units above it"""
        return self._units(self._group_chain(group_id))

    # ------------------------------------------------------------------ effect
    def is_archived(self, node: Node) -> bool:
        if node.kind == "group":
            return node.id in self.archived_groups
        if node.kind == "post":
            return node.id in self.archived_posts
        return False

    def nodes_in_effect_at_post(self, post_id: int) -> Tuple[Node, ...]:
        """Where a grant applies to the post: ROOT, every non-archived group above it (top down), the post itself
        when it is not archived. An id no post can have (0, negative) gets ROOT only."""
        groups = tuple(
            Node.group(group_id) for group_id in reversed(self._post_chains.get(post_id, ()))
            if group_id not in self.archived_groups
        )
        valid = isinstance(post_id, int) and not isinstance(post_id, bool) and post_id >= 1
        own = (Node.post(post_id),) if valid and post_id not in self.archived_posts else ()
        return (ROOT, *groups, *own)

    def posts_in_effect(self, node: Node) -> Optional[FrozenSet[int]]:
        """Posts a grant at the node applies to: None for ROOT (every post), nothing for an archived node"""
        if node.kind == "root":
            return None
        if self.is_archived(node):
            return frozenset()
        return self.posts_under(node)
