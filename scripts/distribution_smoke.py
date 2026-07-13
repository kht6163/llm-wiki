#!/usr/bin/env python3
"""Verify an installed wheel exposes its CLI, version, templates, and static assets."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.resources
import subprocess
import sys
import tarfile
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

ASSET_ROOTS = ("web/templates", "web/static")
MAX_SDIST_BYTES = 10 * 1024 * 1024
REQUIRED_SDIST_FILES = {".env.example", "LICENSE", "SECURITY.md"}
FORBIDDEN_SDIST_PREFIXES = (
    ".venv/",
    ".wheel-smoke/",
    ".worktrees/",
    ".superpowers/",
    "build/",
    "data/",
    "dist/",
    "frontend/coverage/",
    "frontend/node_modules/",
    "htmlcov/",
    "models/",
    "secrets/",
    "vault/",
)
FORBIDDEN_SDIST_SUFFIXES = (
    ".db", ".dll", ".dylib", ".key", ".node", ".pem", ".pyc", ".pyo", ".so",
    ".sqlite", ".sqlite3",
)


def _walk_files(root: Traversable, prefix: str) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for child in root.iterdir():
        relative = f"{prefix}/{child.name}"
        if child.is_file():
            manifest[relative] = hashlib.sha256(child.read_bytes()).hexdigest()
        elif child.is_dir():
            manifest.update(_walk_files(child, relative))
    return manifest


def asset_manifest(package_root: Traversable) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for relative in ASSET_ROOTS:
        asset_root = package_root.joinpath(*relative.split("/"))
        if not asset_root.is_dir():
            raise RuntimeError(f"asset root missing: {relative}")
        manifest.update(_walk_files(asset_root, relative))
    return manifest


def verify_assets(source_root: Traversable, installed_root: Traversable) -> None:
    source = asset_manifest(source_root)
    installed = asset_manifest(installed_root)
    missing = sorted(source.keys() - installed.keys())
    extra = sorted(installed.keys() - source.keys())
    tampered = sorted(
        path for path in source.keys() & installed.keys() if source[path] != installed[path]
    )
    if missing or extra or tampered:
        raise RuntimeError(
            f"wheel asset mismatch: missing={missing}, extra={extra}, tampered={tampered}"
        )


def _forbidden_sdist_member(relative: PurePosixPath) -> bool:
    path = relative.as_posix()
    name = relative.name
    cache_parts = {".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
    secret_env = name == ".env" or (name.startswith(".env.") and name != ".env.example")
    coverage_file = name == ".coverage" or name.startswith(".coverage.") or name == "coverage.xml"
    return (
        any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in FORBIDDEN_SDIST_PREFIXES
        )
        or bool(cache_parts.intersection(relative.parts))
        or secret_env
        or coverage_file
        or path.endswith(FORBIDDEN_SDIST_SUFFIXES)
    )


def verify_sdist(archive: Path, *, max_bytes: int = MAX_SDIST_BYTES) -> None:
    size = archive.stat().st_size
    if size > max_bytes:
        raise RuntimeError(f"sdist size {size} exceeds size limit {max_bytes}")
    seen: set[str] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            full = PurePosixPath(member.name)
            if full.is_absolute() or ".." in full.parts or len(full.parts) < 2:
                raise RuntimeError(f"unsafe sdist member: {member.name}")
            relative = PurePosixPath(*full.parts[1:])
            if _forbidden_sdist_member(relative):
                raise RuntimeError(f"forbidden sdist member: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"non-regular sdist member: {member.name}")
            if member.isfile():
                seen.add(relative.as_posix())
    missing = sorted(REQUIRED_SDIST_FILES - seen)
    if missing:
        raise RuntimeError(f"missing required sdist files: {missing}")


def cli_path(python_executable: str) -> Path:
    return Path(python_executable).parent / "llm-wiki"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version")
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    args = parser.parse_args()

    import llm_wiki

    installed_version = importlib.metadata.version("llm-wiki")
    metadata = importlib.metadata.metadata("llm-wiki")
    if llm_wiki.__version__ != installed_version:
        raise RuntimeError(
            f"package version mismatch: {llm_wiki.__version__} != {installed_version}"
        )
    if args.expected_version and installed_version != args.expected_version:
        raise RuntimeError(
            f"installed version {installed_version} != expected {args.expected_version}"
        )
    if metadata.get("License-Expression") != "MIT":
        raise RuntimeError("wheel is missing the expected MIT license expression")
    project_urls = metadata.get_all("Project-URL") or []
    if not any(value.startswith("Security, ") for value in project_urls):
        raise RuntimeError("wheel is missing the security policy URL")

    installed_root = importlib.resources.files("llm_wiki")
    verify_assets(args.source_package, installed_root)
    verify_sdist(args.sdist)
    cli = cli_path(sys.executable)
    subprocess.run([str(cli), "--help"], check=True, stdout=subprocess.PIPE, text=True)
    print(f"wheel smoke passed for llm-wiki {installed_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
