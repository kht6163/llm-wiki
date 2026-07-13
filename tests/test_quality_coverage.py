"""Release-gate coverage for recently added optional/operations surfaces."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from llm_wiki.config import Settings
from llm_wiki.embedding import DisabledEmbedder
from llm_wiki.graph import _doc_filter_clause
from llm_wiki.runtime import build_context
from llm_wiki.services import audit
from llm_wiki.services import share as share_svc
from llm_wiki.services.auth import create_api_key, list_api_keys
from llm_wiki.services.errors import ConflictError, NotFoundError, ValidationError
from llm_wiki.web import create_web_app


def _csrf(client: TestClient, path: str = "/login") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match
    return match.group(1)


def _login(client: TestClient, username: str = "alice") -> None:
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": "secret12",
            "csrf_token": _csrf(client),
        },
    )
    assert response.status_code == 200


def _logout(client: TestClient) -> None:
    response = client.post("/logout", data={"csrf_token": _csrf(client, "/")})
    assert response.status_code == 200


def test_disabled_embedder_contract_is_explicit():
    embedder = DisabledEmbedder("disabled-test")
    assert embedder.is_loaded is False
    assert embedder.dim == 0
    assert embedder.warm() is None
    for operation in (
        embedder._load,
        lambda: embedder.embed_passages(["text"]),
        lambda: embedder.embed_query("text"),
    ):
        with pytest.raises(RuntimeError, match="embeddings are disabled"):
            operation()


def test_share_tokens_reject_empty_secret_expiry_and_non_path_payload():
    with pytest.raises(ValidationError, match="session secret"):
        share_svc.mint_share_token("", "note.md")

    token = share_svc.mint_share_token("quality-secret", "note.md")
    with pytest.raises(ValidationError, match="expired"):
        share_svc.verify_share_token("quality-secret", token, max_age_s=-1)

    malformed = share_svc._serializer("quality-secret").dumps({"not_path": "note.md"})
    with pytest.raises(ValidationError, match="invalid"):
        share_svc.verify_share_token("quality-secret", malformed)


def test_graph_root_filters_keep_root_and_deduplicate_tags(ctx, principals):
    docs = ctx.docs
    editor = principals["editor"]
    docs.create(editor, "root.md", "[[wanted]] and [[other]]", tags=["root"])
    docs.create(editor, "wanted.md", "wanted", tags=["keep"])
    docs.create(editor, "other.md", "other", tags=["drop"])

    graph = docs.graph(root="root.md", depth=1, tags=["keep", "keep"])
    existing = {node["id"] for node in graph["nodes"] if node.get("exists")}
    assert existing == {"root.md", "wanted.md"}

    clause, params = _doc_filter_clause("/", ["", "keep"])
    assert "tag=?" in clause and params == ["keep"]


def test_audit_via_counts_and_invalid_api_key_metadata(ctx, principals):
    audit.record_tx(ctx.db, actor="alice", via="mcp", action="doc_update", target="a.md")
    counts = audit.via_counts(
        ctx.db,
        since="2000-01-01T00:00:00Z",
        until="2999-01-01T00:00:00Z",
        actions=["doc_update"],
    )
    assert counts["mcp"] == 1

    create_api_key(ctx.db, principals["editor"], "invalid-metadata")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at='not-a-date' WHERE user_id=?",
            (principals["editor"].user_id,),
        )
    listed = list_api_keys(ctx.db, principals["editor"].user_id)
    assert listed[0]["scope"] == "readwrite" and listed[0]["unused"] is True


def test_share_api_rbac_success_and_public_invalid_token(ctx, principals):
    ctx.docs.create(principals["editor"], "shared-api.md", "# Shared")
    client = TestClient(create_web_app(ctx))

    _login(client, "bob")
    denied = client.post(
        "/api/doc/shared-api.md/share",
        data={"csrf_token": _csrf(client, "/")},
    )
    assert denied.status_code == 403

    _logout(client)
    _login(client, "alice")
    allowed = client.post(
        "/api/doc/shared-api.md/share",
        data={"csrf_token": _csrf(client, "/")},
    )
    assert allowed.status_code == 200
    assert allowed.json()["path"] == "shared-api.md"

    invalid = client.get("/share/not-a-token")
    assert invalid.status_code == 400


def test_new_template_failure_and_broken_link_failure_routes(ctx, principals, monkeypatch):
    client = TestClient(create_web_app(ctx))
    _login(client)
    template = client.get("/new", params={"template": "missing"})
    assert template.status_code == 200

    invalid = client.post(
        "/broken-links/create",
        data={"target": "../escape", "csrf_token": _csrf(client, "/broken-links")},
        follow_redirects=False,
    )
    assert invalid.status_code == 303 and invalid.headers["location"] == "/broken-links"

    monkeypatch.setattr(
        ctx.docs, "create", lambda *args, **kwargs: (_ for _ in ()).throw(ConflictError("race"))
    )
    monkeypatch.setattr(
        ctx.docs, "get", lambda *args, **kwargs: (_ for _ in ()).throw(NotFoundError("gone"))
    )
    raced = client.post(
        "/broken-links/create",
        data={"target": "race.md", "csrf_token": _csrf(client, "/broken-links")},
        follow_redirects=False,
    )
    assert raced.status_code == 303 and raced.headers["location"] == "/doc/race.md"


def test_readyz_supports_embedding_disabled_mode(tmp_path):
    settings = Settings(
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "data" / "wiki.db",
        embedding_enabled=False,
        session_secret="quality-ready-secret",
        gui_port=18180,
        mcp_port=18181,
    )
    ctx = build_context(settings, full=True)
    try:
        response = TestClient(create_web_app(ctx)).get("/readyz")
        assert response.status_code == 200
        assert response.json()["embedding_enabled"] is False
    finally:
        ctx.db.close()


def test_docker_smoke_checks_both_ports_with_hardened_runtime(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import docker_smoke

    commands: list[list[str]] = []
    health_urls: list[str] = []

    def run(command, **kwargs):
        commands.append(command)
        stdout = "container-id\n" if command[1] == "run" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def healthy(url: str) -> bool:
        health_urls.append(url)
        return True

    monkeypatch.setattr(docker_smoke.subprocess, "run", run)
    monkeypatch.setattr(docker_smoke, "health_is_ok", healthy)
    docker_smoke.smoke("llm-wiki:test", port=19080, mcp_port=19081, timeout_seconds=1)

    run_command = commands[0]
    assert "--read-only" in run_command
    assert ["--cap-drop", "ALL"] == run_command[
        run_command.index("--cap-drop") : run_command.index("--cap-drop") + 2
    ]
    assert health_urls == [
        "http://127.0.0.1:19080/healthz",
        "http://127.0.0.1:19081/healthz",
    ]
