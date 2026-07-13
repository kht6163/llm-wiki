#!/usr/bin/env python3
"""Start a built image, require /healthz to respond, and always clean it up."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
CONTAINER_NAME = "llm-wiki-ci-smoke"


def health_is_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.load(response)
            return (
                response.status == 200
                and isinstance(payload, dict)
                and payload.get("ok") is True
            )
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return False


def smoke(
    image: str, *, port: int = 18080, mcp_port: int = 18081, timeout_seconds: int = 180
) -> None:
    container_id = ""
    try:
        started = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                CONTAINER_NAME,
                "--publish",
                f"127.0.0.1:{port}:8080",
                "--publish",
                f"127.0.0.1:{mcp_port}:8081",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--cap-drop",
                "ALL",
                "--env",
                f"EMBEDDING_MODEL={TEST_MODEL}",
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        container_id = started.stdout.strip()
        if not container_id:
            raise RuntimeError("docker run returned no container id")

        deadline = time.monotonic() + timeout_seconds
        health_urls = (
            f"http://127.0.0.1:{port}/healthz",
            f"http://127.0.0.1:{mcp_port}/healthz",
        )
        while not all(health_is_ok(url) for url in health_urls):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"container did not become healthy within {timeout_seconds}s")
            time.sleep(2)
    finally:
        if container_id:
            subprocess.run(["docker", "logs", container_id], check=False)
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", container_id], check=False
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--mcp-port", type=int, default=18081)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    smoke(
        args.image,
        port=args.port,
        mcp_port=args.mcp_port,
        timeout_seconds=args.timeout,
    )
    print(f"docker smoke passed for {args.image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
