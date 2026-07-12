from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_distribution_smoke_rejects_missing_wheel_asset(tmp_path: Path):
    from scripts.distribution_smoke import verify_assets

    package_root = tmp_path / "llm_wiki"
    package_root.mkdir()

    with pytest.raises(RuntimeError, match="web/templates/base.html"):
        verify_assets(package_root)


def test_distribution_smoke_accepts_required_wheel_assets(tmp_path: Path):
    from scripts.distribution_smoke import REQUIRED_ASSETS, verify_assets

    package_root = tmp_path / "llm_wiki"
    for relative in REQUIRED_ASSETS:
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("smoke", encoding="utf-8")

    verify_assets(package_root)


def test_distribution_smoke_keeps_cli_in_virtual_environment():
    from scripts.distribution_smoke import cli_path

    assert cli_path("/tmp/wheel-smoke/bin/python") == Path("/tmp/wheel-smoke/bin/llm-wiki")


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
