"""What audit params and scopes of a user, a role or a node are made of: names as they are at write time, the home
node of a user. No FastAPI (auth/ and the CLI use it too)."""
from typing import Optional, Tuple

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node, ROOT
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User

ROOT_NAME = "root"


def home_node(post_id: Optional[int]) -> Node:
    """The home node of a user: their post, the root without one"""
    return Node.post(post_id) if post_id is not None else ROOT


def home_scope(user: User) -> Node:
    """The scope of the audit rows about a user (user, session and certificate events): their home node now"""
    return home_node(user.post_id)


async def user_facts(session: AsyncSession, user_id: int) -> Tuple[str, Node]:
    """(username, home node) of a user by id, plain columns only (never the User entity: its presence column
    properties read user_sessions, see auth/sessions.py)"""
    username, post_id = (await session.exec(select(User.username, User.post_id).where(User.id == user_id))).one()
    return username, home_node(post_id)


async def role_name(session: AsyncSession, role_id: int) -> str:
    """From the identity map when the write loaded the role (assignments do), else one query"""
    return (await session.get(Role, role_id)).name


async def node_name(session: AsyncSession, node: Node) -> str:
    """The name of a post or group as it is now, "root" for the root"""
    if node.kind == "post":
        return (await session.exec(select(Post.name).where(Post.id == node.id))).one()
    if node.kind == "group":
        return (await session.exec(select(ScopeGroup.name).where(ScopeGroup.id == node.id))).one()
    return ROOT_NAME
