from typing import Any, Iterable, Optional, TYPE_CHECKING
from fastapi import status

from app.backend.enums.errors import AccessErrorCode
from app.backend.enums.errors import RequiredPermissions
from app.backend.services.api_errors import ApiError

if TYPE_CHECKING:
    from app.backend.services.permissions import Require


def access_denied(
        code: AccessErrorCode,
        required: Optional["Require"] = None,
        missing: Iterable[str] = (),
        **params: Any,
) -> ApiError:
    """A 403 problem that also says what the route requires and what the caller lacks.

    Usage: raise access_denied(AccessErrorCode.OBSERVATION_NOT_OWNER, required=..., missing=[...])
    """
    return ApiError(
        code,
        required=RequiredPermissions(**required.as_dict()).model_dump() if required is not None else None,
        missing=list(missing),
        **params,
    )
