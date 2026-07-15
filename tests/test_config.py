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


def test_request_max_bytes_defaults_and_bounds():
    one_mib = 1 * 1024 * 1024
    hundred_mib = 100 * 1024 * 1024

    assert Settings().request_max_bytes == 16 * 1024 * 1024
    assert Settings(request_max_bytes=one_mib).request_max_bytes == one_mib
    assert Settings(request_max_bytes=hundred_mib).request_max_bytes == hundred_mib
    with pytest.raises(ValidationError):
        Settings(request_max_bytes=one_mib - 1)
    with pytest.raises(ValidationError):
        Settings(request_max_bytes=hundred_mib + 1)


def test_get_settings_wraps_validation_as_config_error(monkeypatch):
    monkeypatch.setenv("GUI_PORT", "8080")
    monkeypatch.setenv("MCP_PORT", "8080")  # collides with GUI_PORT
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_oidc_disabled_by_default():
    s = Settings()
    assert s.oidc_enabled is False
    assert s.oidc_default_role == "viewer"
    assert s.oidc_scopes == "openid profile email"
    assert s.oidc_auto_provision is True
    assert s.oidc_require_email_verified is True


def test_oidc_enabled_requires_issuer_client_id_redirect():
    with pytest.raises(ValidationError):
        Settings(oidc_enabled=True)
    with pytest.raises(ValidationError):
        Settings(
            oidc_enabled=True,
            oidc_issuer="https://idp.example",
            oidc_client_id="c",
        )
    s = Settings(
        oidc_enabled=True,
        oidc_issuer="https://idp.example",
        oidc_client_id="c",
        oidc_redirect_uri="https://app.example/auth/oidc/callback",
    )
    assert s.oidc_enabled is True


def test_oidc_redirect_uri_https_or_loopback_http():
    base = dict(
        oidc_enabled=True,
        oidc_issuer="https://idp.example",
        oidc_client_id="c",
    )
    assert Settings(
        **base, oidc_redirect_uri="http://127.0.0.1:8080/auth/oidc/callback"
    ).oidc_redirect_uri.endswith("/callback")
    assert Settings(
        **base, oidc_redirect_uri="http://localhost:8080/auth/oidc/callback"
    )
    with pytest.raises(ValidationError):
        Settings(**base, oidc_redirect_uri="http://evil.example/callback")
    with pytest.raises(ValidationError):
        Settings(**base, oidc_redirect_uri="ftp://127.0.0.1/callback")


def test_oidc_default_role_must_be_known():
    with pytest.raises(ValidationError):
        Settings(oidc_default_role="superuser")
    assert Settings(oidc_default_role="Editor").oidc_default_role == "editor"
