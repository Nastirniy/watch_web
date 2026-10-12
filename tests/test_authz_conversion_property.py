"""Property test of the frozen conversion (migrations/support/authz_v2.py) before it freezes: random legacy shapes,
deterministic seed.

Today's semantics (the oracle below, the access model before authorization v2) against the plan evaluated by the
real AuthContext: the plan never widens access, and every narrowing is reported. Compared per post for data and
record permissions, and "held anywhere" for every permission (route reachability; dictionaries and other objects
without a post are compared only there). In this model every narrowing is a permission in `dropped`; the
`differences` of a plan name route behaviours the model does not describe (the upload list, the observation counters,
archived records), each tested on its own in tests/test_authz_conversion.py.
"""
import random
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Grant, Node, ScopeTree
from app.backend.migrations.support import authz_v2 as conv
from app.backend.migrations.support.authz_v2 import LegacyRole, LegacyUser, plan_conversion

SEED = 20261012
BATCHES = 200
USERS_PER_BATCH = 12
POSTS = tuple(range(1, 8))
UNKNOWN = "legacy.unknown"   # a permission row the code no longer knows
UNIVERSE = tuple(sorted(conv.TYPE_CEILINGS["admin"] - conv.REMOVED)) + (UNKNOWN,)
ADMIN_ROLE_ID = 1


def legacy_semantics(user: LegacyUser, roles: Mapping[int, LegacyRole], archived_posts: FrozenSet[int]
                     ) -> Tuple[FrozenSet[str], Callable[[str, int], bool]]:
    """Before authorization v2: the permissions a route accepts (the admin gate applied) and whether a permission
    acts at a post (home post read and write, access rows read or read and write, post.access_all every post,
    admin.true the record exception, archived posts outside the home and row scope)"""
    active = [roles[r] for r in set(user.role_ids) if r in roles and not roles[r].is_deleted]
    held = frozenset().union(*(role.permissions for role in active))
    admin, access_all = conv.ADMIN_TRUE in held, conv.ACCESS_ALL in held
    live = set(held - conv.REMOVED)
    if not admin:
        live -= conv.DEAD_WITHOUT_ADMIN
    if admin and conv.MEDIA_VIEW in live:
        live.add(conv.MEDIA_MONITOR)   # storage statistics and poster status: media.view behind the gate
    rows = dict(user.access)
    places = ([user.post_id] if user.post_id is not None else []) + list(rows)
    readable = {post for post in places if post not in archived_posts}
    writable = {post for post in places if post not in archived_posts and (post == user.post_id or rows.get(post))}

    def at(permission: str, post_id: int) -> bool:
        if permission not in live:
            return False
        if permission in conv.SYSTEM or access_all:
            return True
        if admin and permission in conv.ADMIN_RECORD | conv.ADMIN_ADMINISTRATION:
            return True
        if permission.endswith(".view") or permission in conv.READ_ROW_EXTRA:
            return post_id in readable
        return post_id in writable

    return frozenset(live), at


def planned_context(plan, user_plan, roles: Mapping[int, LegacyRole], tree: ScopeTree) -> AuthContext:
    """The AuthContext the user gets after migration 023: the grants of the planned assignments of roles that are
    not archived (a migrated role holds its permissions without admin.true and post.access_all)"""
    derived = {role.name: role for role in plan.derived_roles}
    grants: List[Grant] = []
    for assignment in user_plan.assignments:
        if isinstance(assignment.role, int):
            role = roles[assignment.role]
            archived, permissions = role.is_deleted, role.permissions - conv.REMOVED
        else:
            role = derived[assignment.role]
            archived, permissions = role.is_deleted, role.permissions
        if not archived:
            grants.append(Grant(0, "r", "R", False, Node.parse(assignment.node), frozenset(permissions)))
    return AuthContext(user=None, grants=tuple(grants), tree=tree)


def random_roles(rng: random.Random) -> Dict[int, LegacyRole]:
    roles = {ADMIN_ROLE_ID: LegacyRole(ADMIN_ROLE_ID, "admin", "Admin", rng.random() < 0.1,
                                       frozenset(UNIVERSE) - {UNKNOWN} | conv.REMOVED)}
    for role_id in range(2, 10):
        permissions = set(rng.sample(UNIVERSE, rng.randint(0, 10)))
        if rng.random() < 0.3:
            permissions.add(conv.ADMIN_TRUE)
        if rng.random() < 0.2:
            permissions.add(conv.ACCESS_ALL)
        roles[role_id] = LegacyRole(role_id, f"r{role_id}", f"Role {role_id}", rng.random() < 0.15,
                                    frozenset(permissions))
    return roles


def random_user(rng: random.Random, user_id: int) -> LegacyUser:
    role_ids = rng.sample(range(2, 10), rng.randint(0, 3))
    if rng.random() < 0.05:
        role_ids.append(ADMIN_ROLE_ID)
    home: Optional[int] = rng.choice(POSTS) if rng.random() < 0.7 else None
    rows = rng.sample(POSTS, rng.randint(0, 3))
    return LegacyUser(user_id, f"u{user_id}", home, tuple(role_ids),
                      tuple((post_id, rng.random() < 0.5) for post_id in rows))


def test_the_conversion_never_widens_and_reports_every_narrowing():
    rng = random.Random(SEED)
    checks = 0
    for batch in range(BATCHES):
        roles = random_roles(rng)
        archived_posts = frozenset(rng.sample(POSTS, rng.randint(0, 2)))
        tree = ScopeTree(group_parent={}, archived_groups=frozenset(), post_group={post: None for post in POSTS},
                         archived_posts=archived_posts)
        # Held anywhere is compared as the plan writes it: a grant on an archived post is held nowhere (spec section
        # 5, R39), a rule of the evaluator the conversion does not report; the comparison per post covers them
        unarchived = ScopeTree(group_parent={}, archived_groups=frozenset(), post_group=tree.post_group,
                               archived_posts=frozenset())
        users = [random_user(rng, batch * 100 + n) for n in range(USERS_PER_BATCH)]
        plan = plan_conversion(users, roles, ADMIN_ROLE_ID, [role.name for role in roles.values()])
        plans = {user_plan.user_id: user_plan for user_plan in plan.users}

        for user in users:
            user_plan = plans[user.id]
            live, legacy_at = legacy_semantics(user, roles, archived_posts)
            auth = planned_context(plan, user_plan, roles, tree)
            held = planned_context(plan, user_plan, roles, unarchived).held_anywhere
            assert auth.held_anywhere <= held
            reported: Set[str] = set(user_plan.dropped)
            shape = (user, sorted(r for r in user.role_ids), sorted(archived_posts))

            assert held <= live, ("widened anywhere", held - live, shape)
            for permission in live - held:
                assert permission in reported, ("unreported loss", permission, shape)
            for permission in UNIVERSE:
                if permission in conv.GLOBAL_VIEW:
                    continue   # objects without a post: held anywhere is what counts
                for post_id in POSTS:
                    before, after = legacy_at(permission, post_id), auth.has_at_post(permission, post_id)
                    checks += 1
                    assert before or not after, ("widened", permission, post_id, shape)
                    if before and not after:
                        assert permission in reported, ("unreported narrowing", permission, post_id, shape)
            # the inferred type always allows what the user holds
            assert held & conv.TYPE_CEILINGS["admin"] <= conv.TYPE_CEILINGS[user_plan.user_type], shape
    assert checks > 100_000
