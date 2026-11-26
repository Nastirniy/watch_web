"""List settings come from the environment as JSON strings: every one must accept that form.

HealthConfig.allowed_hosts had no parser, so the value .env.example ships (MYSETTINGS_HEALTH__ALLOWED_HOSTS=[...])
stopped the API and the worker at startup (found on the test server, 2026-10-19).
"""
import json
import typing

from pydantic import BaseModel

from tests.paths import PROJECT_ROOT
from app.backend import config
from app.backend.config import Settings

ROOT = PROJECT_ROOT
SEQUENCES = (list, tuple, set, dict)


def _sequence_fields():
    for cls in vars(config).values():
        if isinstance(cls, type) and issubclass(cls, BaseModel) and cls.__module__ == config.__name__:
            for name, info in cls.model_fields.items():
                kinds = {typing.get_origin(info.annotation)} | {typing.get_origin(a) for a in typing.get_args(info.annotation)}
                if kinds & set(SEQUENCES):
                    yield cls, name, info


def test_every_list_setting_accepts_a_json_string():
    fields = list(_sequence_fields())
    assert fields, "no list setting found: the scan is broken"
    for cls, name, info in fields:
        default = info.get_default(call_default_factory=True)
        value = list(default) if isinstance(default, (tuple, set)) else default
        parsed = getattr(cls(**{name: json.dumps(value)}), name)
        assert parsed == default, f"{cls.__name__}.{name} does not read its JSON form"


def test_list_values_of_env_example_load(monkeypatch):
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = [line.split("=", 1) for line in lines if line.startswith("MYSETTINGS_") and "=[" in line]
    assert ("MYSETTINGS_HEALTH__ALLOWED_HOSTS", '["127.0.0.1", "localhost", "::1"]') in [tuple(p) for p in pairs]
    for key, value in pairs:
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.health.allowed_hosts == ["127.0.0.1", "localhost", "::1"]
