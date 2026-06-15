"""Configuration validation (batch A): bad values fail fast at construction, and
get_settings() surfaces them as a readable ConfigError."""
import pytest
from pydantic import ValidationError

from llm_wiki.config import ConfigError, Settings, get_settings


def test_duplicate_ports_rejected():
    with pytest.raises(ValidationError):
        Settings(gui_port=9000, mcp_port=9000)


def test_port_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Settings(gui_port=0)
    with pytest.raises(ValidationError):
        Settings(mcp_port=70000)


def test_bad_log_level_rejected():
    with pytest.raises(ValidationError):
        Settings(log_level="LOUD")


def test_empty_host_rejected():
    with pytest.raises(ValidationError):
        Settings(host="   ")


def test_log_level_is_normalized():
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_shutdown_grace_defaults_and_bounds():
    assert Settings().shutdown_grace_s == 25
    assert Settings(shutdown_grace_s=10).shutdown_grace_s == 10
    with pytest.raises(ValidationError):
        Settings(shutdown_grace_s=0)
    with pytest.raises(ValidationError):
        Settings(shutdown_grace_s=301)


def test_get_settings_wraps_validation_as_config_error(monkeypatch):
    monkeypatch.setenv("GUI_PORT", "8080")
    monkeypatch.setenv("MCP_PORT", "8080")  # collides with GUI_PORT
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError):
            get_settings()
    finally:
        get_settings.cache_clear()
