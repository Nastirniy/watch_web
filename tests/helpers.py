"""Factories for test data. Every object is created directly in the database."""
import itertools
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.enums.general import Permissions
from app.backend.models.user import User
from app.backend.models.role import Role, Permission, RolePermission, UserRole
from app.backend.models.post import Post
from app.backend.models.user_post_access import UserPostAccess
from app.backend.models.observation import Observation, Category, SignalType, Tag
from app.backend.models.media import MediaFile

_counter = itertools.count(1)


def _next() -> int:
    return next(_counter)


def perm_name(permission) -> str:
    return permission.value if isinstance(permission, Permissions) else str(permission)


async def make_post(db: AsyncSession, name: str = None) -> Post:
    n = _next()
    post = Post(name=name or f"Post {n}", code=f"P{n:04d}")
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


async def make_role(db: AsyncSession, perms: Iterable, name: str = None) -> Role:
    n = _next()
    role = Role(name=name or f"role_{n}", display_name=f"Role {n}")
    db.add(role)
    await db.flush()
    names = [perm_name(p) for p in perms]
    if names:
        result = await db.exec(select(Permission).where(Permission.name.in_(names)))
        found = {p.name: p for p in result.all()}
        missing = [x for x in names if x not in found]
        assert not missing, f"permissions not seeded: {missing}"
        for x in names:
            db.add(RolePermission(role_id=role.id, permission_id=found[x].id))
    await db.commit()
    await db.refresh(role)
    return role


async def make_user(
        db: AsyncSession,
        perms: Iterable = (),
        post_id: Optional[int] = None,
        extra_access: Optional[Dict[int, bool]] = None,
        role: Optional[Role] = None,
        username: str = None,
) -> User:
    n = _next()
    user = User(
        pki_certificate_cn_hash=f"cn-hash-{n}",
        pki_certificate_serial_hash=f"serial-hash-{n}",
        pki_certificate_fingerprint_hash=f"fp-hash-{n}",
        username=username or f"user_{n}",
        username_alias=f"User {n}",
        post_id=post_id,
    )
    db.add(user)
    await db.flush()
    if role is None:
        role = await make_role(db, perms)
    db.add(UserRole(user_id=user.id, role_id=role.id))
    for extra_post_id, can_write in (extra_access or {}).items():
        db.add(UserPostAccess(user_id=user.id, post_id=extra_post_id, can_write=can_write))
    await db.commit()
    await db.refresh(user)
    return user


async def make_admin(db: AsyncSession, post_id: Optional[int] = None) -> User:
    """User holding the seeded admin role (every permission)"""
    result = await db.exec(select(Role).where(Role.name == "admin"))
    admin_role = result.one()
    return await make_user(db, role=admin_role, post_id=post_id)


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


async def make_observation(db: AsyncSession, user: User, post: Post, signal_type_id: int) -> Observation:
    observation = Observation(
        user_id=user.id,
        post_id=post.id,
        signal_type_id=signal_type_id,
        frequency_min=Decimal("100.000"),
        frequency_max=Decimal("200.000"),
        frequency_center=Decimal("150.000"),
        bandwidth=Decimal("100.000"),
        observed_at=datetime.now(timezone.utc),
    )
    db.add(observation)
    await db.commit()
    await db.refresh(observation)
    return observation


async def make_media(db: AsyncSession, observation: Observation, user: User) -> MediaFile:
    n = _next()
    media = MediaFile(
        observation_id=observation.id,
        user_id=user.id,
        original_filename=f"file{n}.jpg",
        stored_filename=f"stored_{n}.jpg",
        file_path=f"2026/09/02/obs_{observation.id}/stored_{n}.jpg",
        mime_type="image/jpeg",
        file_extension="jpg",
        media_type="image",
        file_hash=f"{n:064d}",
        file_size=123,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)
    return media


def observation_payload(post_id: int, signal_type_id: int) -> dict:
    return {
        "post_id": post_id,
        "signal_type_id": signal_type_id,
        "frequency_min": "100.000",
        "frequency_max": "200.000",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
