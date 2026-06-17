"""Hardening checks: header-safe Content-Disposition (RFC 5987 + ASCII fallback) and
control-character rejection in document path normalization."""
import pytest

from llm_wiki.util import PathError, content_disposition_attachment, normalize_rel_path


def test_content_disposition_rfc5987_unicode():
    h = content_disposition_attachment("노트.md")
    assert h.startswith("attachment;")
    assert "filename*=UTF-8''" in h and "%" in h  # Korean is percent-encoded, not raw


def test_content_disposition_strips_header_injection():
    h = content_disposition_attachment('a"b\r\nX-Evil: 1.md')
    assert "\r" not in h and "\n" not in h          # no CRLF survives into the header value
    assert '"b' not in h                            # a quote can't terminate filename early


def test_normalize_rel_path_rejects_control_chars():
    for bad in ("evil\nX-Inject: 1.md", "a\rb.md", "tab\there.md", "nul\x00.md"):
        with pytest.raises(PathError):
            normalize_rel_path(bad)
    assert normalize_rel_path("ok/note.md") == "ok/note.md"  # clean paths still pass
