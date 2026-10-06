"""A setting no model declares (removed, renamed or misspelled) is reported at startup instead of ignored silently."""
from app.backend.config import Settings


def test_shipped_config_has_no_unknown_settings(capsys):
    Settings()
    assert "unknown setting" not in capsys.readouterr().out, "app/config.toml carries a key no setting declares"


def test_removed_and_misspelled_settings_are_reported(monkeypatch, capsys):
    monkeypatch.setenv("MYSETTINGS_SECURITY__SESSION_EXPIRE_HOURS", "8")      # removed
    monkeypatch.setenv("MYSETTINGS_SECURITY__ALLOWED_ORIGINS", '["*"]')       # removed with CORS
    monkeypatch.setenv("MYSETTINGS_STORAGE__IMGPROXY__SALTT", "x")            # misspelled, nested section
    monkeypatch.setenv("MYSETTINGS_SECURITY__SESSION_IDLE_TIMEOUT_MINUTES", "45")
    settings = Settings()
    out = capsys.readouterr().out
    assert "unknown setting security.session_expire_hours is ignored" in out
    assert "unknown setting security.allowed_origins is ignored" in out
    assert "unknown setting storage.imgproxy.saltt is ignored" in out
    assert "session_idle_timeout_minutes" not in out
    assert settings.security.session_idle_timeout_minutes == 45
