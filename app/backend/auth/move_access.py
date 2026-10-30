"""What a move of a post or a group needs, without the web framework: the routes answer from it (auth/privileges.py,
check_covers), and a queued move runs through it without a request (services/moves.py).

move_requirements: the permissions a move needs and where (spec of authorization v3: X.view AND X.update at both ends,
and post.view and post.update for what a group takes along). A post needs them covering its old and its new location,
a group or a brigade covering its old and its new parent; a grant on the node itself covers neither.

ungrantable_by_move: the H1 rule (auth/privileges.py, "the scope tree is access too"): what the destination puts in
effect at the moved subtree that the caller could not grant there. The checks read the scope tree again inside the
write, after the locks of the move.
"""
from dataclasses import replace
from typing import Dict, FrozenSet, Iterable, List, Set, Tuple

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext, load_scope_tree
from app.backend.auth.integrity import switchable_permissions
from app.backend.auth.scope import Node
from app.backend.enums.general import MoveNodeKind, Permissions, RoleKind
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.user import User


def move_requirements(kind: MoveNodeKind, old: Node, new: Node) -> Tuple[Tuple[Permissions, Node], ...]:
    """(permission, node it must cover) of a move, in the order the routes ask"""
    if MoveNodeKind(kind) is MoveNodeKind.POST:
        return tuple((permission, location) for location in (old, new)
                     for permission in (Permissions.POST_UPDATE, Permissions.POST_VIEW))
    return (
        (Permissions.SCOPE_GROUP_VIEW, new), (Permissions.SCOPE_GROUP_VIEW, old),
        *((Permissions.SCOPE_GROUP_UPDATE, location) for location in (old, new)),
        *((permission, location) for location in (old, new)
          for permission in (Permissions.POST_UPDATE, Permissions.POST_VIEW)),
    )


def view_requirements(kind: MoveNodeKind) -> Tuple[Permissions, ...]:
    """What a move needs, read at the node itself: who may follow a move (GET /moves/{id}) holds these there"""
    if MoveNodeKind(kind) is MoveNodeKind.POST:
        return (Permissions.POST_VIEW, Permissions.POST_UPDATE)
    return (Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE, Permissions.POST_VIEW, Permissions.POST_UPDATE)


def not_covered(auth: AuthContext, permissions: Iterable[str], node: Node) -> Set[str]:
    return {name for name in set(permissions) if not auth.covers(name, node)}


async def _assignments_at(session: AsyncSession, nodes: Iterable[Node]) -> List[Tuple[int, str, FrozenSet[str]]]:
    """(user id, scope key, permissions) of every tree assignment at the nodes of a tree role that is not archived,
    held by a user who is not archived; the permissions are what some type could put in effect
    (switchable_permissions). System assignments act at the root, which a move never adds."""
    keys = sorted({node.key for node in nodes})
    if not keys:
        return []
    rows = (await session.exec(
        select(RoleAssignment.id, RoleAssignment.user_id, RoleAssignment.scope_key, Permission.name)
        .join(Role, Role.id == RoleAssignment.role_id)
        .join(User, User.id == RoleAssignment.user_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .where(RoleAssignment.scope_key.in_(keys), Role.is_deleted == False,  # noqa: E712
               Role.kind == RoleKind.TREE.value, User.is_deleted == False)  # noqa: E712
    )).all()
    heads: Dict[int, Tuple[int, str]] = {}
    names: Dict[int, Set[str]] = {}
    for assignment_id, user_id, scope_key, name in rows:
        heads[assignment_id] = (user_id, scope_key)
        held = names.setdefault(assignment_id, set())
        if name:
            held.add(name)
    return [(*heads[assignment_id], switchable_permissions(RoleKind.TREE, names[assignment_id]))
            for assignment_id in sorted(heads)]


async def newly_given(session: AsyncSession, added: Iterable[Node], already: Iterable[Node]) -> FrozenSet[str]:
    """Every permission the assignments at the added nodes give their user that the user does not hold already
    through an assignment at the already nodes (where the target node gets its grants from now)"""
    added_keys = {node.key for node in added}
    if not added_keys:
        return frozenset()
    held: Dict[int, Set[str]] = {}
    new: List[Tuple[int, FrozenSet[str]]] = []
    for user_id, scope_key, permissions in await _assignments_at(session, [*added, *already]):
        if scope_key in added_keys:
            new.append((user_id, permissions))
        else:
            held.setdefault(user_id, set()).update(permissions)
    return frozenset().union(*(permissions - held.get(user_id, set()) for user_id, permissions in new))


async def committed(session: AsyncSession, auth: AuthContext) -> AuthContext:
    """The caller with the scope tree read inside the write (after the route's locks)"""
    return replace(auth, tree=await load_scope_tree(session))


def live_nodes(tree, nodes: Iterable[Node]) -> List[Node]:
    return [node for node in nodes if not tree.is_archived(node)]


async def ungrantable_by_move(session: AsyncSession, auth: AuthContext, moved: Node, new_parent: Node) -> List[str]:
    """A move of the post or group moved under new_parent (a group, the root): every assignment at the new parent's
    ancestors (itself included) that is not already above the moved node, nor inside it, nor at an archived node,
    applies at the moved subtree after the move. The permissions the caller could not grant at the moved node, as it
    stands before the move, of what each of them newly gives its user there (newly_given: less what the user already
    holds at the moved node), sorted."""
    current = await committed(session, auth)
    tree = current.tree
    above = set(tree.ancestors(moved)[1:])
    added = [node for node in live_nodes(tree, tree.ancestors(new_parent))
             if node not in above and not tree.contains(moved, node)]
    given = await newly_given(session, added, live_nodes(tree, tree.ancestors(moved)))
    return sorted(not_covered(current, given, moved))
