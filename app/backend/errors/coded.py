"""Coded failures without the web framework: what services, filters and request validators raise.

A code is a problem type of enums/errors.py; its params are exactly the ones the type declares (a param that
the type does not declare, or a missing required one, is a programming error: TypeError). The HTTP side
(errors/problems.py) turns a CodedError into a problem response with ApiError.from_coded.
"""
from typing import Any, Dict, Union, get_args, get_origin

from pydantic_core import PydanticCustomError

from app.backend.enums.errors import FieldCode, ProblemType

# RFC 9457 members and the localization key; params never use these names
RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance", "code"})
# Members of an item of a validation_error besides its params
FIELD_MEMBERS = frozenset({"code", "detail", "pointer", "parameter", "location"})


def is_optional(annotation: Any) -> bool:
    return get_origin(annotation) is Union and type(None) in get_args(annotation)


def check_params(code: Union[ProblemType, FieldCode], params: Dict[str, Any], partial: bool = False) -> None:
    """Params must be exactly the ones the code declares (required ones present unless partial)"""
    reserved = FIELD_MEMBERS if isinstance(code, FieldCode) else RESERVED_MEMBERS
    unknown = set(params) - set(code.params)
    clash = reserved.intersection(params)
    missing = set() if partial else {name for name, annotation in code.params.items()
                                     if not is_optional(annotation) and name not in params}
    if unknown or clash or missing:
        raise TypeError(f"{code.value}: unknown params {sorted(unknown)}, reserved {sorted(clash)}, missing {sorted(missing)}")


class CodedError(ValueError):
    """Domain failure raised by a service with its problem type; the route may add the params only it knows
    (ApiError.from_coded). A ValueError so callers that only know "rejected input" keep working."""

    def __init__(self, code: ProblemType, **params: Any):
        check_params(code, params, partial=True)
        self.code = code
        self.params = params
        super().__init__(f"{code.value}: {params}")


def invalid(code: FieldCode, **params: Any) -> PydanticCustomError:
    """Item of a validation_error, for request model validators: raise invalid(FieldErrorCode.USERNAME_RESERVED)"""
    check_params(code, params)
    # The detail is rendered here so lists read like in ApiError; pydantic only fills {key} left in a template
    return PydanticCustomError(code.value, code.render(params), params)
