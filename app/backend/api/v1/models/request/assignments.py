from typing import Optional

from pydantic import Field, PositiveInt, model_validator

from app.backend.auth.scope import Node, NodeKind
from app.backend.enums.errors import FieldErrorCode
from app.backend.errors.coded import invalid
from app.backend.utils.validators import SecureBaseModel


class ScopeRef(SecureBaseModel):
    """A node of the scope tree: the root (no id), a scope group or a post (its id)"""
    kind: NodeKind = Field(description="root: the whole system; group: a scope group and everything below it; post: one post")
    id: Optional[PositiveInt] = Field(default=None, description="Id of the group or the post; left out for the root")

    @model_validator(mode="after")
    def id_matches_kind(self):
        if self.kind == "root" and self.id is not None:
            raise invalid(FieldErrorCode.SCOPE_ID_NOT_ALLOWED)
        if self.kind != "root" and self.id is None:
            raise invalid(FieldErrorCode.SCOPE_ID_REQUIRED)
        return self

    @property
    def node(self) -> Node:
        return Node(self.kind, self.id)


class SystemAssignmentCreate(SecureBaseModel):
    """A system role for the user: it acts on the whole system, without a node"""
    role_id: PositiveInt


class AssignmentCreate(SecureBaseModel):
    """A role for the user at a node of the scope tree"""
    role_id: PositiveInt
    scope: ScopeRef
