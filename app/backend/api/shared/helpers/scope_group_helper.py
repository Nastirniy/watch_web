"""Scope groups of every kind (plain groups, corps, brigades): what /scope-groups, /corps and /brigades share.

Visibility before existence: a group outside every subtree where the caller holds scope_group.view answers like an
unknown id. Names are unique within a parent, case-insensitive, archived groups included (parent_key 0 is the root,
shared by root plain groups and corps).
"""
from typing import Optional

from fastapi import Request
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import Permissions, ScopeGroupKind
from app.backend.errors.problems import ApiError
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.audit import create_audit_log
from app.backend.utils.row_locks import lock_first


async def name_taken(session: AsyncSession, name: str, parent_id: Optional[int], exclude_id: Optional[int] = None) -> bool:
    """Case-insensitive, archived groups included: the name is unique within its parent (parent_key 0 is the root)"""
    query = select(ScopeGroup.id).where(
        ScopeGroup.parent_key == (parent_id or 0), func.lower(ScopeGroup.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.where(ScopeGroup.id != exclude_id)
    return (await session.exec(query)).first() is not None


def check_visible(auth: AuthContext, group_id: int) -> None:
    """Scope before existence: a group outside every subtree where the caller holds scope_group.view answers like an
    unknown id (an outsider learns nothing about ids)"""
    if not auth.covers(Permissions.SCOPE_GROUP_VIEW, Node.group(group_id)):
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)


async def get_unit(session: AsyncSession, auth: AuthContext, kind: ScopeGroupKind, unit_id: int,
                   include_deleted: bool = False, lock: bool = False) -> ScopeGroup:
    """The group of this kind; 404 scope_group_not_found when it is unknown, archived (unless include_deleted),
    outside the caller's scope_group.view or of another kind (a brigade id on /corps/{id}). lock=True locks it as
    the first statement of the write (utils/row_locks.py)."""
    check_visible(auth, unit_id)
    query = select(ScopeGroup).where(ScopeGroup.id == unit_id, ScopeGroup.kind == ScopeGroupKind(kind).value)
    if not include_deleted:
        query = query.where(ScopeGroup.is_deleted == False)  # noqa: E712
    group = await lock_first(session, query) if lock else (await session.exec(query)).first()
    if group is None:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    return group


async def audit_group(request: Request, session: AsyncSession, auth: AuthContext, action: str, group: ScopeGroup,
                      old_values: Optional[dict]) -> None:
    """One audit row (resource_type scope_group) for a change of a group of any kind"""
    await create_audit_log(
        session=session,
        action=action,
        resource_type="scope_group",
        resource_id=group.id,
        user_id=auth.user.id,
        old_values=old_values,
        new_values=group.model_dump(),
        details={"message": f"User {auth.user.username} {action}d {group.kind} '{group.name}'"},
        **client_info(request, auth)
    )
