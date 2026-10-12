"""Factories for test data. Every object is created directly in the database."""
import itertools
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, Optional

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions, UserType
from app.backend.errors.problems import PROBLEM_JSON
from app.backend.migrations.support.authz_v2 import LegacyRole, LegacyUser, REMOVED, plan_conversion
from app.backend.models.user import User
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.observation import Observation, ObservationFrequency, Category, SignalType, Tag
from app.backend.models.media import MediaFile

_counter = itertools.count(1)

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


async def make_group(db: AsyncSession, name: str = None, parent_id: Optional[int] = None) -> ScopeGroup:
    n = _next()
    group = ScopeGroup(name=name or f"Group {n}", parent_id=parent_id, parent_key=parent_id or 0)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


async def make_role(db: AsyncSession, perms: Iterable, name: str = None) -> Role:
    n = _next()
    role = Role(name=name or f"role_{n}", display_name=f"Role {n}")
    db.add(role)
    await db.flush()
    await _grant(db, role, [perm_name(p) for p in perms])
    await db.commit()
    await db.refresh(role)
    return role


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


async def assign(db: AsyncSession, user: User, role: Role, node: Node) -> RoleAssignment:
    """An assignment of the role to the user at the node (root, scope group or post)"""
    assignment = RoleAssignment(user_id=user.id, role_id=role.id, **scope_columns(node))
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return assignment


def _new_user(n: int, username: Optional[str], post_id: Optional[int], user_type: str) -> User:
    return User(
        pki_certificate_cn_hash=f"cn-hash-{n}",
        pki_certificate_serial_hash=f"serial-hash-{n}",
        pki_certificate_fingerprint_hash=f"fp-hash-{n}",
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
    """A user shaped like the old access model, written the way migration 023 converts it: one role (perms, which
    may name ADMIN_TRUE and ACCESS_ALL, or an existing role), the home post post_id and access rows
    extra_access {post id: can_write} go through the frozen conversion (plan_conversion), and its plan is written:
    the role itself or derived roles (<role>_system / _posts / _view, named against the roles of the database) at
    the planned nodes, and the inferred user type unless user_type is given. A user who would hold nothing gets no
    assignment."""
    n = _next()
    user = _new_user(n, username, post_id, UserType.VIEWER.value)
    db.add(user)
    await db.flush()
    if role is None:
        names = frozenset(perm_name(p) for p in perms)
        role = await make_role(db, names - REMOVED)
    else:
        names = await _permission_names(db, role.id)
    legacy_role = LegacyRole(id=role.id, name=role.name, display_name=role.display_name, is_deleted=role.is_deleted,
                             permissions=names)
    admin_role_id = (await db.exec(select(Role.id).where(Role.builtin_key == UserType.ADMIN.value))).first()
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
    for planned in user_plan.assignments:
        role_id = planned.role if isinstance(planned.role, int) else derived[planned.role].id
        db.add(RoleAssignment(user_id=user.id, role_id=role_id, **scope_columns(Node.parse(planned.node))))
    user.user_type = UserType(user_type).value if user_type is not None else user_plan.user_type
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def make_admin(db: AsyncSession, post_id: Optional[int] = None) -> User:
    """A user of type admin holding the built-in admin role (every permission) at the root"""
    admin_role = (await db.exec(select(Role).where(Role.builtin_key == UserType.ADMIN.value))).one()
    user = _new_user(_next(), None, post_id, UserType.ADMIN.value)
    db.add(user)
    await db.flush()
    db.add(RoleAssignment(user_id=user.id, role_id=admin_role.id, **scope_columns(ROOT)))
    await db.commit()
    await db.refresh(user)
    return user


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
