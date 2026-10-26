from typing import List, Literal, Optional, Sequence

from pydantic import Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.response.posts import UnitRefResponse
from app.backend.auth.scope import ScopeTree
from app.backend.enums.general import ScopeGroupKind, UserType
from app.backend.models.user import User
from app.backend.services.user_units import UserUnits, users_units
from app.backend.utils.validators import PlainTextModel, ResponseDatetime, SecureBaseModel


class PostRefResponse(PlainTextModel):
    """A post: its id, today's name and code"""
    id: int
    name: str
    code: str


class HomeRefResponse(PlainTextModel):
    """The node a user belongs to: a post today (a scope group joins later)"""
    kind: Literal["post", "group"]
    id: int
    name: str
    group_kind: Optional[ScopeGroupKind] = Field(default=None, description="Kind of the group when kind is group; null for a post")


class UserResponse(SecureBaseModel):
    id: int
    username: str
    username_alias: str
    post_id: Optional[int]
    user_type: UserType = Field(description="The type of the user (GET /user-types): the pages of the web client")
    is_active: bool
    created_at: ResponseDatetime
    updated_at: Optional[ResponseDatetime]
    last_login_at: Optional[ResponseDatetime]
    is_online: bool = False
    last_activity_at: Optional[ResponseDatetime] = None
    is_deleted: bool = False
    home: Optional[HomeRefResponse] = Field(default=None, description="The node the user belongs to now; null without a home")
    post: Optional[PostRefResponse] = Field(default=None, description="The home post of the user (post_id with its name and code); null without one")
    brigade: Optional[UnitRefResponse] = Field(default=None, description="Brigade the home sits in now; null: outside any brigade")
    corps: Optional[UnitRefResponse] = Field(default=None, description="Corps the home sits in now (directly or through a brigade); null: outside any corps")


class UserPublicResponse(SecureBaseModel):
    """Public projection of a user, used as nested creator info (observation.user, media.user)"""
    id: int
    username: str
    username_alias: str
    is_online: bool = False
    last_activity_at: Optional[ResponseDatetime] = None
    is_deleted: bool = False


_UNIT_FIELDS = ("home", "post", "brigade", "corps")


def units_fields(units: UserUnits) -> dict:
    """The home, post, brigade and corps fields of a user answer"""
    return {
        "home": HomeRefResponse.model_validate(units.home, from_attributes=True) if units.home else None,
        "post": PostRefResponse.model_validate(units.post, from_attributes=True) if units.post else None,
        "brigade": UnitRefResponse.model_validate(units.brigade, from_attributes=True) if units.brigade else None,
        "corps": UnitRefResponse.model_validate(units.corps, from_attributes=True) if units.corps else None,
    }


async def users_to_response(session: AsyncSession, tree: ScopeTree, users: Sequence[User]) -> List[UserResponse]:
    """User answers with their home and units (at most two queries for the names of the page)"""
    units = await users_units(session, tree, users)
    return [_user_response(user, units[user.id]) for user in users]


def _user_response(user: User, units: UserUnits) -> UserResponse:
    # Not model_validate(user): User.post is the relationship, the post of the answer is the PostRefResponse
    own = {name: getattr(user, name) for name in UserResponse.model_fields if name not in _UNIT_FIELDS}
    return UserResponse.model_validate({**own, **units_fields(units)})


async def user_to_response(session: AsyncSession, tree: ScopeTree, user: User) -> UserResponse:
    return (await users_to_response(session, tree, [user]))[0]
