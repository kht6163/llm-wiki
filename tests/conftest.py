import pytest

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.auth import Principal, create_user

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # small + fast for tests


@pytest.fixture
def ctx(tmp_path):
    settings = Settings(
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "data" / "wiki.db",
        embedding_model=TEST_MODEL,
        gui_port=8088,
        mcp_port=8089,
        session_secret="test-secret",
    )
    return build_context(settings, full=True)


@pytest.fixture
def principals(ctx):
    admin_id = create_user(ctx.db, "admin", "secret12", "admin")
    editor_id = create_user(ctx.db, "alice", "secret12", "editor")
    viewer_id = create_user(ctx.db, "bob", "secret12", "viewer")
    return {
        "admin": Principal(admin_id, "admin", "admin"),
        "editor": Principal(editor_id, "alice", "editor"),
        "viewer": Principal(viewer_id, "bob", "viewer"),
    }
