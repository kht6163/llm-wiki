from server import build_test_app


def test_app_uses_requested_root_and_seeds_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_WIKI_E2E_PORT", "41000")
    monkeypatch.setenv("LLM_WIKI_E2E_MCP_PORT", "41001")

    app = build_test_app(tmp_path)

    assert (tmp_path / "data" / "wiki.db").is_file()
    assert (tmp_path / "vault" / "start.md").read_text(encoding="utf-8") == (
        "# 시작 안내\n\n키보드 탐색 기준 문서"
    )
    assert "/login" in {route.path for route in app.routes}
