"""Production refuses to start with a placeholder secret; the health host check follows the environment."""
import pytest

from app.backend.bootstrap.deployment import PLACEHOLDER_SECRETS, check_deployment
from app.backend.config import Settings


def _settings(monkeypatch, environment, secret=None, **env):
    monkeypatch.setenv("MYSETTINGS_APP__ENVIRONMENT", environment)
    if secret is not None:
        monkeypatch.setenv("MYSETTINGS_SECURITY__SECRET_KEY", secret)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


@pytest.mark.parametrize("secret", ["", "   ", "your-secret-key-change-this-in-production", "CHANGE_ME", "change_me"])
def test_production_refuses_an_empty_or_placeholder_secret(monkeypatch, secret):
    settings = _settings(monkeypatch, "production", secret)
    with pytest.raises(RuntimeError, match="secret_key"):
        check_deployment(settings)


def test_production_refuses_the_secret_of_the_committed_config(monkeypatch):
    """No variable set: the placeholder of app/config.toml is what the process would sign tokens with"""
    monkeypatch.delenv("MYSETTINGS_SECURITY__SECRET_KEY", raising=False)
    settings = _settings(monkeypatch, "production")
    assert settings.security.secret_key in PLACEHOLDER_SECRETS
    with pytest.raises(RuntimeError):
        check_deployment(settings)


def test_production_accepts_a_real_secret_without_a_warning(monkeypatch):
    settings = _settings(monkeypatch, "production", "0123456789abcdef" * 4)
    messages = []
    check_deployment(settings, warn=messages.append)
    assert messages == []


def test_development_warns_once_and_starts_with_the_placeholder(monkeypatch):
    settings = _settings(monkeypatch, "development", "CHANGE_ME")
    messages = []
    check_deployment(settings, warn=messages.append)
    assert len(messages) == 1 and "development" in messages[0]


def test_health_host_check_defaults_to_on_in_production_only(monkeypatch):
    monkeypatch.delenv("MYSETTINGS_HEALTH__ENABLE_HOST_CHECK", raising=False)
    assert _settings(monkeypatch, "production", "x" * 32).health.enable_host_check is True
    assert _settings(monkeypatch, "development", "x" * 32).health.enable_host_check is False


def test_health_host_check_can_be_set_explicitly(monkeypatch):
    off = _settings(monkeypatch, "production", "x" * 32, MYSETTINGS_HEALTH__ENABLE_HOST_CHECK="false")
    assert off.health.enable_host_check is False


def test_shipped_config_allows_health_only_from_loopback(monkeypatch):
    assert "*" not in _settings(monkeypatch, "production", "x" * 32).health.allowed_hosts
