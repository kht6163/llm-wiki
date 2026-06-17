"""Startup robustness: CLI port overrides are re-validated (they bypass Settings'
construction-time validators), and a tilde in LOG_FILE is expanded for the file
handler the same way it is for the parent mkdir."""
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_wiki import _cli_impl
from llm_wiki.config import ConfigError, Settings
from llm_wiki.logconf import configure_logging


def _settings(**over):
    base = dict(vault_path="/tmp/v", db_path="/tmp/d/w.db", embedding_model="m",
                session_secret="x", gui_port=8080, mcp_port=8081)
    base.update(over)
    return Settings(**base)


def test_serve_override_rejects_equal_ports():
    s = _settings()
    with pytest.raises(ConfigError):
        _cli_impl._apply_serve_overrides(s, SimpleNamespace(host=None, gui_port=9000, mcp_port=9000))


def test_serve_override_rejects_out_of_range_port():
    s = _settings()
    with pytest.raises(ConfigError):
        _cli_impl._apply_serve_overrides(s, SimpleNamespace(host=None, gui_port=99999, mcp_port=None))


def test_serve_override_applies_distinct_ports():
    s = _settings()
    out = _cli_impl._apply_serve_overrides(s, SimpleNamespace(host=None, gui_port=9000, mcp_port=9001))
    assert out.gui_port == 9000 and out.mcp_port == 9001
    # No flags -> untouched (same object, no needless re-validation).
    assert _cli_impl._apply_serve_overrides(s, SimpleNamespace(host=None, gui_port=None, mcp_port=None)) is s


def test_log_file_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))          # ~ -> tmp_path on POSIX
    try:
        configure_logging("INFO", "~/logs/app.log")
        # The handler opens its file on construction, at the EXPANDED path...
        assert (tmp_path / "logs" / "app.log").exists()
        # ...not a literal "./~/..." directory.
        assert not (Path.cwd() / "~").exists()
    finally:
        for h in list(logging.getLogger("llm_wiki").handlers):
            h.close()
        configure_logging("INFO", "")                  # drop the file handler
