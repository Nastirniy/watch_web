"""Factories for test data. Every object is created directly in the database."""
import itertools
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, Optional

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.certificates import CN_SCHEME_CURRENT, certificate_values, hash_certificate_data
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.user_types import USER_TYPES, boundary
from app.backend.bootstrap.permissions import role_kind_of
from app.backend.enums.general import BuiltinRole, CertificateSource, Permissions, RoleKind, UserType
from app.backend.errors.problems import PROBLEM_JSON
from app.backend.migrations.support.authz_v2 import LegacyRole, LegacyUser, REMOVED, plan_conversion
from app.backend.migrations.support.authz_v3 import V2Assignment, V2Role, V2User, plan_v3
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.observation import Observation, ObservationFrequency, Category, SignalType, Tag
from app.backend.models.media import MediaFile

_counter = itertools.count(1)
# Users whose type the test chose (make_user with user_type, make_admin): assign() and give() never change it
_EXPLICIT_TYPES: set = set()

# The legacy flags (gone from the permission catalog with migration 023): make_user still takes them, the frozen
# conversion turns them into assignments exactly like the migration did
ADMIN_TRUE = "admin.true"
ACCESS_ALL = "post.access_all"


async def errors_are_problems(response: httpx.Response) -> None:
    """httpx response hook of the test client: every error the API answers is a problem (API.md)"""
    if response.status_code >= 400:
        content_type = response.headers.get("content-type", "")
        assert content_type == PROBLEM_JSON, (
            f"{response.request.method} {response.request.url.path} answered {response.status_code} "
            f"without application/problem+json ({content_type or 'no content type'})")


def _next() -> int:
    return next(_counter)


def perm_name(permission) -> str:
    return permission.value if isinstance(permission, Permissions) else str(permission)


async def make_post(db: AsyncSession, name: str = None, group_id: Optional[int] = None) -> Post:
    n = _next()
    post = Post(name=name or f"Post {n}", code=f"P{n:04d}", group_id=group_id)
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


async def make_group(db: AsyncSession, name: str = None, parent_id: Optional[int] = None, kind: str = "group") -> ScopeGroup:
    n = _next()
    group = ScopeGroup(name=name or f"Group {n}", parent_id=parent_id, parent_key=parent_id or 0, kind=kind)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def make_role(db: AsyncSession, perms: Iterable, name: str = None, kind: Optional[RoleKind] = None,
                    allow_mixed: bool = False) -> Role:
    """A custom role holding perms. Its kind follows the permissions (system when all are system ones); a mix of
    both levels is refused unless allow_mixed (only a bug, or v2 data before the conversion, makes one)."""
    n = _next()
    names = [perm_name(p) for p in perms]
    inferred = role_kind_of(names)
    assert inferred is not None or allow_mixed or not names, f"a role mixing tree and system permissions: {names}"
    role = Role(name=name or f"role_{n}", display_name=f"Role {n}", kind=(kind or inferred or RoleKind.TREE).value)
    db.add(role)
    await db.flush()
    await _grant(db, role, [perm_name(p) for p in perms])
    await db.commit()
    await db.refresh(role)
    return role


async def give(db: AsyncSession, user: User, perms: Iterable, node: Node = ROOT) -> list:
    """Roles giving the user perms at node, split by level as authorization v3 requires: the tree permissions in a
    tree role assigned at node, the system ones in a system role (a system assignment: node must be the root).
    Returns the roles, the tree one first."""
    names = [perm_name(p) for p in perms]
    tree = [name for name in names if role_kind_of([name]) is RoleKind.TREE]
    system = [name for name in names if name not in tree]
    roles = []
    if tree or not system:
        role = await make_role(db, tree)
        await assign(db, user, role, node)
        roles.append(role)
    if system:
        assert node == ROOT, f"system permissions act only at the root: {system}"
        role = await make_role(db, system)
        await assign(db, user, role, ROOT)
        roles.append(role)
    return roles


async def _grant(db: AsyncSession, role: Role, names: Iterable[str]) -> None:
    names = sorted(set(names))
    if not names:
        return
    result = await db.exec(select(Permission).where(Permission.name.in_(names)))
    found = {p.name: p for p in result.all()}
    missing = [x for x in names if x not in found]
    assert not missing, f"permissions not seeded: {missing}"
    for x in names:
        db.add(RolePermission(role_id=role.id, permission_id=found[x].id))


async def _permission_names(db: AsyncSession, role_id: int) -> frozenset:
    rows = await db.exec(select(Permission.name).join(RolePermission, RolePermission.permission_id == Permission.id)
                         .where(RolePermission.role_id == role_id))
    return frozenset(rows.all())


async def assign(db: AsyncSession, user: User, role: Role, node: Node):
    """An assignment of the role to the user at the node (root, scope group or post); a system role is a system
    assignment (the node must be the root)"""
    if role.kind == RoleKind.SYSTEM.value:
        assert node == ROOT, "a system role has no node"
        assignment = SystemRoleAssignment(user_id=user.id, role_id=role.id)
    else:
        assignment = RoleAssignment(user_id=user.id, role_id=role.id, **scope_columns(node))
    db.add(assignment)
    await db.flush()
    await _fit_type(db, user)
    await db.commit()
    await db.refresh(assignment)
    return assignment


def _new_user(n: int, username: Optional[str], post_id: Optional[int], user_type: str) -> User:
    return User(
        username=username or f"user_{n}",
        username_alias=f"User {n}",
        post_id=post_id,
        user_type=user_type,
    )


async def make_user(
        db: AsyncSession,
        perms: Iterable = (),
        post_id: Optional[int] = None,
        extra_access: Optional[Dict[int, bool]] = None,
        role: Optional[Role] = None,
        username: str = None,
        user_type: Optional[UserType] = None,
) -> User:
    """A user shaped like the old access model, written the way migrations 023 and 030 convert it: one role (perms,
    which may name ADMIN_TRUE and ACCESS_ALL, or an existing role), the home post post_id and access rows
    extra_access {post id: can_write} go through the frozen v2 conversion (plan_conversion), and its plan is
    written: the role itself or derived roles (<role>_system / _posts / _view, named against the roles of the
    database) at the planned nodes, and the inferred user type unless user_type is given. Then the frozen v3
    conversion (plan_v3) runs on what was written: roles get their kind, a role mixing both levels gives its system
    permissions to <role>_system, root assignments of system roles become system assignments, the v2 type its v3
    name. A user who would hold nothing gets no assignment."""
    n = _next()
    user = _new_user(n, username, post_id, UserType.VIEWER.value)
    db.add(user)
    await db.flush()
    if role is None:
        names = frozenset(perm_name(p) for p in perms)
        role = await make_role(db, names - REMOVED, allow_mixed=True)
    else:
        names = await _permission_names(db, role.id)
    legacy_role = LegacyRole(id=role.id, name=role.name, display_name=role.display_name, is_deleted=role.is_deleted,
                             permissions=names)
    admin_role_id = None        # the v2 built-in admin role is gone: make_admin makes administrators
    legacy_user = LegacyUser(id=user.id, username=user.username, post_id=post_id, role_ids=(role.id,),
                             access=tuple(sorted((extra_access or {}).items())))
    taken = (await db.exec(select(Role.name))).all()
    plan = plan_conversion([legacy_user], {role.id: legacy_role}, admin_role_id, taken)

    derived: Dict[str, Role] = {}
    for part in plan.derived_roles:
        row = Role(name=part.name, display_name=part.display_name, is_deleted=part.is_deleted)
        db.add(row)
        await db.flush()
        await _grant(db, row, part.permissions)
        derived[part.name] = row
    (user_plan,) = plan.users
    written = []
    for planned in user_plan.assignments:
        role_id = planned.role if isinstance(planned.role, int) else derived[planned.role].id
        row = RoleAssignment(user_id=user.id, role_id=role_id, **scope_columns(Node.parse(planned.node)))
        db.add(row)
        written.append(row)
    await db.flush()
    await _convert_v3(db, user, written, user_plan.user_type)
    if user_type is not None:
        user.user_type = UserType(user_type).value
        _EXPLICIT_TYPES.add(user.id)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _fit_type(db: AsyncSession, user: User) -> None:
    """A user whose type the test did not choose gets the first type of the catalog whose boundary covers all their
    assignments (as the v2 conversion inferred types): such tests are about scope, not types, and before v3 the
    ceiling of the type did not act at evaluation. A user with a chosen type keeps it."""
    if user.id in _EXPLICIT_TYPES:
        return
    rows = (await db.exec(
        select(Role.kind, Permission.name).select_from(RoleAssignment)
        .join(Role, Role.id == RoleAssignment.role_id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(RoleAssignment.user_id == user.id)
        .union_all(
            select(Role.kind, Permission.name).select_from(SystemRoleAssignment)
            .join(Role, Role.id == SystemRoleAssignment.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(SystemRoleAssignment.user_id == user.id))
    )).all()
    tree = {name for kind, name in rows if kind == RoleKind.TREE.value}
    system = {name for kind, name in rows if kind == RoleKind.SYSTEM.value}
    current = UserType(user.user_type)
    if tree <= boundary(current, RoleKind.TREE) and system <= boundary(current, RoleKind.SYSTEM):
        return
    fitting = next(t for t in USER_TYPES
                   if tree <= boundary(t, RoleKind.TREE) and system <= boundary(t, RoleKind.SYSTEM))
    await db.execute(User.__table__.update().where(User.id == user.id).values(user_type=fitting.value))
    user.user_type = fitting.value


async def _convert_v3(db: AsyncSession, user: User, written: list, v2_type: str) -> None:
    """plan_v3 over the roles and assignments make_user wrote (custom roles only), applied to the database"""
    role_ids = sorted({row.role_id for row in written})
    roles = {}
    for role_id in role_ids:
        row = (await db.exec(select(Role).where(Role.id == role_id))).one()
        roles[role_id] = V2Role(row.id, row.name, row.display_name, row.is_deleted, row.builtin_key,
                                await _permission_names(db, row.id))
    assignments = [V2Assignment(row.id, user.id, row.role_id, row.scope_key) for row in written]
    taken = (await db.exec(select(Role.name))).all()
    plan = plan_v3(roles, assignments, [V2User(user.id, v2_type)], taken)
    for role_id, kind in plan.role_kinds.items():
        row = (await db.exec(select(Role).where(Role.id == role_id))).one()
        if row.builtin_key is None:
            row.kind = kind
            db.add(row)
    for role_id, removed in plan.role_permissions_removed.items():
        ids = (await db.exec(select(Permission.id).where(Permission.name.in_(sorted(removed))))).all()
        await db.execute(RolePermission.__table__.delete().where(
            RolePermission.role_id == role_id, RolePermission.permission_id.in_(ids)))
    splits = {}
    for split in plan.split_roles:
        row = Role(name=split.name, display_name=split.display_name, is_deleted=split.is_deleted,
                   kind=RoleKind.SYSTEM.value)
        db.add(row)
        await db.flush()
        await _grant(db, row, split.permissions)
        splits[split.name] = row
    if plan.moved_assignment_ids:
        await db.execute(RoleAssignment.__table__.delete().where(RoleAssignment.id.in_(plan.moved_assignment_ids)))
    for planned in plan.system_assignments:
        role_id = splits[planned.role].id if isinstance(planned.role, str) else planned.role
        db.add(SystemRoleAssignment(user_id=planned.user_id, role_id=role_id))
    user.user_type = plan.user_types[user.id]
    await db.flush()


async def builtin_role(db: AsyncSession, key: BuiltinRole) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == key.value))).one()


async def make_admin(db: AsyncSession, post_id: Optional[int] = None) -> User:
    """A user of type system_administrator holding its base roles: administrator at the root (every tree
    permission) and the system role system_administrator (every system permission)"""
    user = _new_user(_next(), None, post_id, UserType.SYSTEM_ADMINISTRATOR.value)
    db.add(user)
    await db.flush()
    _EXPLICIT_TYPES.add(user.id)
    db.add(RoleAssignment(user_id=user.id, role_id=(await builtin_role(db, BuiltinRole.ADMINISTRATOR)).id,
                          **scope_columns(ROOT)))
    db.add(SystemRoleAssignment(user_id=user.id,
                                role_id=(await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)).id))
    await db.commit()
    await db.refresh(user)
    return user


async def make_certificate_row(
        db: AsyncSession,
        user: User,
        source: CertificateSource = CertificateSource.UPLOADED,
        cert: Optional[x509.Certificate] = None,
        **values,
) -> UserCertificate:
    """A certificate row of the user. With cert: its hashes (scheme 2) and, for issued and uploaded rows, its plain
    values, as register_certificate stores them (legacy and configured rows hold only the hashes); without one
    made-up unique hashes that match no certificate. values set any column (cn_hash_scheme=1, revoked_at=...)."""
    n = _next()
    if cert is not None:
        parsed = certificate_values(cert.public_bytes(serialization.Encoding.PEM))
        cn_hash, serial_hash, fingerprint_hash = await hash_certificate_data(parsed.cn, parsed.serial, parsed.fingerprint)
        fields = dict(label=parsed.cn[:64], subject_cn=parsed.cn, serial=parsed.serial, not_before=parsed.not_before,
                      not_after=parsed.not_after)
    else:
        cn_hash, serial_hash, fingerprint_hash = f"cn-hash-{n}", f"serial-hash-{n}", f"fp-hash-{n}"
        fields = dict(label=f"Device {n}", subject_cn=f"Device {n}", serial=f"{n:x}")
    if source in (CertificateSource.LEGACY, CertificateSource.CONFIGURED):
        fields = dict(label="")
    fields.update(cn_hash=cn_hash, cn_hash_scheme=CN_SCHEME_CURRENT, serial_hash=serial_hash,
                  fingerprint_hash=fingerprint_hash)
    fields.update(values)
    row = UserCertificate(user_id=user.id, source=source.value, **fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def make_dictionary(db: AsyncSession) -> Dict[str, int]:
    n = _next()
    category = Category(name=f"Category {n}", alias=f"cat{n}")
    db.add(category)
    await db.flush()
    signal_type = SignalType(code=f"ST{n}", name=f"Signal type {n}", category_id=category.id)
    tag = Tag(name=f"tag{n}")
    db.add(signal_type)
    db.add(tag)
    await db.commit()
    await db.refresh(signal_type)
    await db.refresh(tag)
    return {"category_id": category.id, "signal_type_id": signal_type.id, "tag_id": tag.id}


async def make_observation(db: AsyncSession, user: User, post: Post, signal_type_id: int, bands: Iterable = None) -> Observation:
    """Observation with bands given as (kind, min, max) tuples; one rc band 100-200 MHz by default"""
    observation = Observation(
        user_id=user.id,
        post_id=post.id,
        signal_type_id=signal_type_id,
        observed_at=datetime.now(timezone.utc),
    )
    db.add(observation)
    await db.flush()
    for kind, low, high in bands or [("rc", "100.000", "200.000")]:
        low, high = Decimal(low), Decimal(high)
        db.add(ObservationFrequency(
            observation_id=observation.id, kind=kind,
            frequency_min=low, frequency_max=high, frequency_center=(low + high) / 2,
        ))
    await db.commit()
    await db.refresh(observation)
    return observation


MEDIA_KINDS = {
    "image": ("jpg", "image/jpeg"),
    "video": ("mp4", "video/mp4"),
    "audio": ("mp3", "audio/mpeg"),
}


async def make_media(db: AsyncSession, observation: Observation, user: User, media_type: str = "image", **fields) -> MediaFile:
    """Media row. Videos start with poster_status = pending like a real upload; pass poster_status=... to override."""
    n = _next()
    ext, mime = MEDIA_KINDS[media_type]
    values = dict(
        observation_id=observation.id,
        user_id=user.id,
        original_filename=f"file{n}.{ext}",
        stored_filename=f"stored_{n}.{ext}",
        file_path=f"2026/09/02/obs_{observation.id}/stored_{n}.{ext}",
        mime_type=mime,
        file_extension=ext,
        media_type=media_type,
        file_hash=f"{n:064d}",
        file_size=123,
    )
    if media_type == "video":
        values["poster_status"] = "pending"
    values.update(fields)
    media = MediaFile(**values)
    db.add(media)
    await db.commit()
    await db.refresh(media)
    return media


def observation_payload(post_id: int, signal_type_id: int) -> dict:
    return {
        "post_id": post_id,
        "signal_type_id": signal_type_id,
        "frequencies": [{"kind": "rc", "frequency_min": "100.000", "frequency_max": "200.000"}],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
