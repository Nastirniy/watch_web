"""Where a user belongs: the home node and the units (brigade, corps) around it, for the user records.

The units come from the scope tree every request loads; the names are read today (one query for the posts of a page,
one for the groups).
"""
from dataclasses import dataclass
from typing import Dict, Optional, Sequence

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node, ScopeTree
from app.backend.enums.general import ScopeGroupKind
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User


@dataclass(frozen=True)
class UnitRef:
    id: int
    name: str


@dataclass(frozen=True)
class PostRef:
    id: int
    name: str
    code: str


@dataclass(frozen=True)
class HomeRef:
    """kind: post or group; group_kind: the kind of the group (corps, brigade, group), None for a post"""
    kind: str
    id: int
    name: str
    group_kind: Optional[str]


@dataclass(frozen=True)
class UserUnits:
    home: Optional[HomeRef] = None
    post: Optional[PostRef] = None
    brigade: Optional[UnitRef] = None
    corps: Optional[UnitRef] = None


def _home_of(user: User) -> Optional[Node]:
    """The node the user belongs to: the home post (a home group joins here)"""
    return Node.post(user.post_id) if user.post_id is not None else None


def _units_of(tree: ScopeTree, node: Node) -> Dict[str, int]:
    if node.kind == "post":
        return tree.units_of_post(node.id)
    return tree.units_of_group(node.id)


async def users_units(session: AsyncSession, tree: ScopeTree, users: Sequence[User]) -> Dict[int, UserUnits]:
    """user id -> the home node and the units around it (at most two queries for a page of users)"""
    homes = {user.id: node for user in users if (node := _home_of(user)) is not None}
    post_ids = {node.id for node in homes.values() if node.kind == "post"}
    units = {user_id: _units_of(tree, node) for user_id, node in homes.items()}

    posts: Dict[int, PostRef] = {}
    if post_ids:
        rows = await session.exec(select(Post.id, Post.name, Post.code).where(Post.id.in_(sorted(post_ids))))
        posts = {post_id: PostRef(post_id, name, code) for post_id, name, code in rows.all()}

    group_ids = {unit_id for found in units.values() for unit_id in found.values()}
    group_ids |= {node.id for node in homes.values() if node.kind == "group"}
    groups: Dict[int, tuple] = {}
    if group_ids:
        rows = await session.exec(select(ScopeGroup.id, ScopeGroup.name, ScopeGroup.kind).where(
            ScopeGroup.id.in_(sorted(group_ids))))
        groups = {group_id: (name, kind) for group_id, name, kind in rows.all()}

    def unit(user_id: int, kind: ScopeGroupKind) -> Optional[UnitRef]:
        unit_id = units.get(user_id, {}).get(kind.value)
        return UnitRef(unit_id, groups[unit_id][0]) if unit_id in groups else None

    result: Dict[int, UserUnits] = {}
    for user in users:
        node = homes.get(user.id)
        home: Optional[HomeRef] = None
        post: Optional[PostRef] = None
        if node is not None and node.kind == "post" and node.id in posts:
            post = posts[node.id]
            home = HomeRef("post", post.id, post.name, None)
        elif node is not None and node.kind == "group" and node.id in groups:
            name, kind = groups[node.id]
            home = HomeRef("group", node.id, name, kind)
        result[user.id] = UserUnits(home=home, post=post, brigade=unit(user.id, ScopeGroupKind.BRIGADE),
                                    corps=unit(user.id, ScopeGroupKind.CORPS))
    return result
