#!/usr/bin/env python3
"""Verify an installed wheel exposes its CLI, version, templates, and static assets."""

from __future__ import annotations

import argparse
import importlib.metadata
import subprocess
import sys
from pathlib import Path

REQUIRED_ASSETS = (
    "web/templates/base.html",
    "web/templates/view.html",
    "web/templates/edit.html",
    "web/static/style.css",
    "web/static/editor.js",
    "web/static/vendor/cytoscape.min.js",
    "web/static/vendor/hljs.bundle.js",
    "web/static/vendor/hljs-theme.css",
    "web/static/vendor/md-editor.bundle.js",
    "web/static/vendor/md-editor.bundle.css",
)


def verify_assets(package_root: Path) -> None:
    missing = [relative for relative in REQUIRED_ASSETS if not (package_root / relative).is_file()]
    if missing:
        raise RuntimeError("wheel is missing required assets: " + ", ".join(missing))


def cli_path(python_executable: str) -> Path:
    return Path(python_executable).parent / "llm-wiki"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version")
    args = parser.parse_args()

    import llm_wiki

    installed_version = importlib.metadata.version("llm-wiki")
    if llm_wiki.__version__ != installed_version:
        raise RuntimeError(
            f"package version mismatch: {llm_wiki.__version__} != {installed_version}"
        )
    if args.expected_version and installed_version != args.expected_version:
        raise RuntimeError(
            f"installed version {installed_version} != expected {args.expected_version}"
        )

    package_root = Path(llm_wiki.__file__).resolve().parent
    verify_assets(package_root)
    cli = cli_path(sys.executable)
    subprocess.run([str(cli), "--help"], check=True, stdout=subprocess.PIPE, text=True)
    print(f"wheel smoke passed for llm-wiki {installed_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
