"""The .env loader: onboarding convenience without a new dependency."""

from __future__ import annotations

import os

import pytest

from app.config import _load_env_file, build_settings


def test_env_file_loads_values_but_shell_wins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "DEEPSEEK_API_KEY=sk-from-file\n"
        "AIGATE_LOG_LEVEL = 'DEBUG'\n"
        "AIGATE_ADMIN_TOKEN=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AIGATE_LOG_LEVEL", raising=False)
    monkeypatch.setenv("AIGATE_ADMIN_TOKEN", "from-shell")

    _load_env_file(env_file)

    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-file"
    assert os.environ["AIGATE_LOG_LEVEL"] == "DEBUG"
    # never override what the shell already set
    assert os.environ["AIGATE_ADMIN_TOKEN"] == "from-shell"


def test_missing_env_file_is_a_no_op(tmp_path):
    _load_env_file(tmp_path / "does-not-exist.env")  # must not raise


def test_provider_key_from_environment_wires_default_route(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    settings = build_settings()

    names = {provider.name for provider in settings.providers}
    assert "deepseek" in names
    assert settings.routes["default"] == ("deepseek:deepseek-chat",)


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_flag_parsing(monkeypatch, value):
    monkeypatch.setenv("AIGATE_SEMANTIC_CACHE", value)
    assert build_settings().semantic_cache is False
