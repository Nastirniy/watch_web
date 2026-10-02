"""Error bodies are RFC 9457 problems typed by enums/errors.py; their OpenAPI schemas are generated in
app/backend/openapi_problems.py. RequiredPermissions is re-exported for the auth responses."""
from app.backend.enums.errors import RequiredPermissions  # noqa: F401
