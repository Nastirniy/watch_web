from typing import Iterable, Optional, TYPE_CHECKING
from fastapi import HTTPException, status

from app.backend.enums.general import AccessErrorCode
from app.backend.routes.models.response.error_response import ErrorDetail, RequiredPermissions

if TYPE_CHECKING:
    from app.backend.services.permissions import Require


def access_denied(
        code: AccessErrorCode,
        message: str,
        required: Optional["Require"] = None,
        missing: Iterable[str] = (),
) -> HTTPException:
    """Build a 403 with the structured body {"detail": {code, message, required, missing}}.

    Usage: raise access_denied(AccessErrorCode.NOT_OWNER, "You can only modify your own observations")
    """
    detail = ErrorDetail(
        code=code.value,
        message=message,
        required=RequiredPermissions(**required.as_dict()) if required is not None else None,
        missing=list(missing),
    )
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail.model_dump())
