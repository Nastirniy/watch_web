from typing import Optional

from pydantic import Field

from app.backend.auth.scope import NodeKind
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class AssignmentRole(SecureBaseModel):
    id: int
    name: str
    display_name: str
    is_builtin: bool = Field(description="A built-in role of a user type (owned by the system)")
    is_deleted: bool = Field(description="The role is archived: the assignment grants nothing until it is restored")


class ScopeResponse(SecureBaseModel):
    """The node an assignment applies to (and everything below it)"""
    kind: NodeKind
    id: Optional[int] = Field(description="Id of the group or the post; null at the root")
    name: str = Field(description="Name of the group or the post; the application name at the root")


# Built from auth/system_assignments.py SystemAssignmentView
class SystemAssignmentResponse(SecureBaseModel):
    """A system role a user holds: it acts on the whole system (no node)"""
    id: int
    role: AssignmentRole
    created_at: ResponseDatetime


# Built from auth/assignments.py AssignmentView: AssignmentResponse.model_validate(view, from_attributes=True)
class AssignmentResponse(SecureBaseModel):
    """A role a user holds at one node of the scope tree: it applies to the node and everything below it"""
    id: int
    role: AssignmentRole
    scope: ScopeResponse
    created_at: ResponseDatetime
