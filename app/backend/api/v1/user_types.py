from typing import List

from fastapi import APIRouter, Depends
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.response.user_types import UserTypeResponse, UserTypeRole
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
    """The catalog of user types, in catalog order: the base role, the base permissions and the ceiling of each."""
    rows = await session.exec(select(Role).where(Role.builtin_key.is_not(None)))
    base_roles = {role.builtin_key: role for role in rows.all()}
    return [
        UserTypeResponse(
            key=spec.key,
            display_name=spec.display_name,
            description=spec.description,
            base_role=UserTypeRole.model_validate(base_roles[spec.key.value]),
            base_permissions=sorted(spec.base_permissions),
            allowed_permissions=sorted(spec.allowed_permissions),
        )
        for spec in USER_TYPES.values()
    ]
