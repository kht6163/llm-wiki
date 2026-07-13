from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _asset_trees(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    for root in (source, installed):
        for relative, content in (
            ("web/templates/nested/page.html", b"template"),
            ("web/static/nested/app.js", b"static"),
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return source, installed


@pytest.mark.parametrize("mutation", ["missing", "extra", "tampered"])
def test_distribution_smoke_rejects_any_wheel_asset_regression(tmp_path: Path, mutation: str):
    from scripts.distribution_smoke import verify_assets

    source, installed = _asset_trees(tmp_path)
    if mutation == "missing":
        (installed / "web/static/nested/app.js").unlink()
    elif mutation == "extra":
        (installed / "web/static/extra.js").write_bytes(b"extra")
    else:
        (installed / "web/templates/nested/page.html").write_bytes(b"changed")

    with pytest.raises(RuntimeError, match=mutation):
        verify_assets(source, installed)


def test_distribution_smoke_accepts_exact_wheel_asset_tree(tmp_path: Path):
    from scripts.distribution_smoke import verify_assets

    source, installed = _asset_trees(tmp_path)
    verify_assets(source, installed)


@pytest.mark.parametrize(
    "member",
    [
        "llm_wiki-0.31.1/.wheel-smoke/bin/python",
        "llm_wiki-0.31.1/frontend/node_modules/pkg/index.js",
        "llm_wiki-0.31.1/data/llm_wiki.db",
        "llm_wiki-0.31.1/native/addon.node",
        "llm_wiki-0.31.1/secrets/token.txt",
        "llm_wiki-0.31.1/config/signing.key",
    ],
)
def test_sdist_smoke_rejects_forbidden_members(tmp_path: Path, member: str):
    from scripts.distribution_smoke import verify_sdist

    archive = tmp_path / "package.tar.gz"
    payload = tmp_path / "payload"
    payload.write_bytes(b"bad")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname=member)

    with pytest.raises(RuntimeError, match="forbidden sdist member"):
        verify_sdist(archive)


def test_sdist_smoke_rejects_oversized_archive(tmp_path: Path):
    from scripts.distribution_smoke import verify_sdist

    archive = tmp_path / "package.tar.gz"
    archive.write_bytes(b"x" * 11)

    with pytest.raises(RuntimeError, match="exceeds size limit"):
        verify_sdist(archive, max_bytes=10)


def test_sdist_smoke_requires_env_example(tmp_path: Path):
    from scripts.distribution_smoke import verify_sdist

    archive = tmp_path / "package.tar.gz"
    payload = tmp_path / "payload"
    payload.write_bytes(b"readme")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="llm_wiki-0.31.1/README.md")

    with pytest.raises(RuntimeError, match=r"missing required sdist files:.*\.env\.example"):
        verify_sdist(archive)


def test_distribution_smoke_keeps_cli_in_virtual_environment():
    from scripts.distribution_smoke import cli_path

    assert cli_path("/tmp/wheel-smoke/bin/python") == Path("/tmp/wheel-smoke/bin/llm-wiki")


def test_docker_smoke_accepts_health_details(monkeypatch):
    from scripts import docker_smoke

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok": true, "model_loaded": true}'

    monkeypatch.setattr(docker_smoke.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    assert docker_smoke.health_is_ok("http://127.0.0.1:8081/healthz") is True


def test_docker_smoke_cleans_up_when_health_never_starts(monkeypatch):
    from scripts import docker_smoke

    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            return subprocess.CompletedProcess(command, 0, stdout="container-id\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_smoke.subprocess, "run", run)
    monkeypatch.setattr(docker_smoke, "health_is_ok", Mock(return_value=False))
    monkeypatch.setattr(docker_smoke.time, "sleep", Mock())

    with pytest.raises(RuntimeError, match="did not become healthy"):
        docker_smoke.smoke("test-image", timeout_seconds=0)

    assert ["docker", "logs", "container-id"] in calls
    assert ["docker", "rm", "--force", "--volumes", "container-id"] in calls


def test_docker_smoke_rejects_failed_container_start(monkeypatch):
    from scripts import docker_smoke

    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.CalledProcessError(125, command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_smoke.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError):
        docker_smoke.smoke("test-image", timeout_seconds=1)

    assert not any(command[1] in {"logs", "rm"} for command in calls)
