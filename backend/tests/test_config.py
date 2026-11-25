"""Unit tests for ``app.core.config.Settings`` / ``get_settings``."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings
from tests.conftest import TEST_SECRET_KEY


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_defaults(monkeypatch) -> None:
    # conftest pins some of these for fast tests; clear them to assert real defaults.
    for var in (
        "LLM_TIMEOUT_SECONDS",
        "LLM_PROBE_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
        "LLM_REGISTRY_MAX_CLIENTS",
        "ALLOW_PRIVATE_LLM_HOSTS",
        "ALLOW_PRIVATE_CRAWL_HOSTS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(SECRET_KEY=TEST_SECRET_KEY)

    assert settings.LLM_TIMEOUT_SECONDS == 60.0
    assert settings.LLM_PROBE_TIMEOUT_SECONDS == 20.0
    assert settings.LLM_MAX_RETRIES == 2
    assert settings.LLM_REGISTRY_MAX_CLIENTS == 64
    assert settings.ALLOW_PRIVATE_LLM_HOSTS is True
    assert settings.ALLOW_PRIVATE_CRAWL_HOSTS is False
    assert settings.LOG_LEVEL == "INFO"


def test_secret_key_is_wrapped_in_secret_str() -> None:
    settings = Settings(SECRET_KEY=TEST_SECRET_KEY)

    assert isinstance(settings.SECRET_KEY, SecretStr)
    assert TEST_SECRET_KEY not in repr(settings)
    assert TEST_SECRET_KEY not in str(settings.SECRET_KEY)


def test_secret_key_is_trimmed() -> None:
    settings = Settings(SECRET_KEY=f"  {TEST_SECRET_KEY}\n")

    assert settings.SECRET_KEY.get_secret_value() == TEST_SECRET_KEY


@pytest.mark.parametrize("bad_key", ["", "   ", "not-a-fernet-key", "a" * 20])
def test_invalid_secret_key_rejected(bad_key: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(SECRET_KEY=bad_key)

    assert "SECRET_KEY" in str(excinfo.value)


def test_missing_secret_key_rejected(monkeypatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert "SECRET_KEY" in str(excinfo.value)


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("ALLOW_PRIVATE_LLM_HOSTS", "false")

    settings = Settings(SECRET_KEY=TEST_SECRET_KEY)

    assert settings.LLM_TIMEOUT_SECONDS == 7.5
    assert settings.ALLOW_PRIVATE_LLM_HOSTS is False
