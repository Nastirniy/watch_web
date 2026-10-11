"""Checks of the deployment itself, before anything else starts (no FastAPI here).

Production never runs with a secret that is empty or published in the repository: tokens signed with it
could be minted by anyone. Outside production the same values are allowed, with one line saying so.
"""
from collections.abc import Callable

from app.backend.config import Settings
from app.backend.enums.general import AppEnvironment
from app.backend.utils.logging_config import get_main_logger

# The value committed in app/config.toml and the one the example files use
PLACEHOLDER_SECRETS = frozenset({
    "your-secret-key-change-this-in-production",
    "change_me",
})


def secret_is_placeholder(secret: str) -> bool:
    return not secret.strip() or secret.strip().lower() in PLACEHOLDER_SECRETS


def check_deployment(settings: Settings, warn: Callable[[str], None] | None = None) -> None:
    """Raise RuntimeError when production would run with an empty or placeholder secret_key; otherwise
    (not production) log one warning line when the environment is not production."""
    production = settings.app.environment == AppEnvironment.PRODUCTION.value
    if production:
        if secret_is_placeholder(settings.security.secret_key):
            raise RuntimeError(
                "security.secret_key is empty or a placeholder: set MYSETTINGS_SECURITY__SECRET_KEY "
                "(python -c \"import secrets; print(secrets.token_hex(32))\") before running in production"
            )
        return
    warn = warn or get_main_logger().warning
    warn(
        f"Running in the {settings.app.environment} environment: the login takes certificate data from a JSON "
        "body and the health host check is off unless set; never expose this process (production is "
        "MYSETTINGS_APP__ENVIRONMENT=production)"
    )
