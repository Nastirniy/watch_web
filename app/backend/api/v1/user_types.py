from typing import List

from fastapi import APIRouter, Depends
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.response.user_types import UserTypeBaseRole, UserTypeResponse
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.user_types import USER_TYPES
from app.backend.database import get_session
from app.backend.enums.routes import UserTypeRoutes
from app.backend.errors.openapi import problems
from app.backend.models.role import Role

router = APIRouter(prefix=UserTypeRoutes.BASE.value, tags=[UserTypeRoutes.tag()])


@router.get(
    UserTypeRoutes.ROOT.value,
    response_model=List[UserTypeResponse],
    responses=problems(),
)
async def list_user_types(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
):
    """The catalog of user types, in catalog order: the base roles and the boundaries (tree and system) of each."""
    rows = await session.exec(select(Role).where(Role.builtin_key.is_not(None)))
    base_roles = {role.builtin_key: role for role in rows.all()}
    return [
        UserTypeResponse(
            key=spec.key,
            display_name=spec.display_name,
            description=spec.description,
            base_roles=[UserTypeBaseRole(
                id=base_roles[base.role.value].id, name=base_roles[base.role.value].name,
                display_name=base_roles[base.role.value].display_name, kind=base_roles[base.role.value].kind,
                at_root=base.at_root) for base in spec.base_roles],
            tree_boundary=sorted(spec.tree_boundary),
            system_boundary=sorted(spec.system_boundary),
        )
        for spec in USER_TYPES.values()
    ]
