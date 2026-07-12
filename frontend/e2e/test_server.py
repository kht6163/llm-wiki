import socket

import pytest
from server import bind_listener, build_test_app


def test_listener_keeps_ephemeral_port_owned_until_closed():
    listener = bind_listener()
    contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        host, port = listener.getsockname()
        assert host == "127.0.0.1"
        assert port > 0
        with pytest.raises(OSError):
            contender.bind((host, port))
    finally:
        contender.close()
        listener.close()


def test_app_uses_requested_root_and_seeds_documents(tmp_path):
    app = build_test_app(tmp_path, gui_port=41000, mcp_port=41001)

    try:
        assert (tmp_path / "data" / "wiki.db").is_file()
        assert (tmp_path / "vault" / "start.md").read_text(encoding="utf-8") == (
            "# 시작 안내\n\n키보드 탐색 기준 문서"
        )
        assert "/login" in {route.path for route in app.routes}
    finally:
        app.state.e2e_db.close()
