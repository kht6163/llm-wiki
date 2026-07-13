"""Real-process MCP streamable-HTTP smoke test.

This complements the fast in-process FastMCP tests by crossing both uvicorn sockets,
the HTTP transport, Bearer authentication, JSON-RPC serialization, and graceful
process shutdown.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(urls: list[str], process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    pending = set(urls)
    while pending and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"llm-wiki exited before HTTP readiness ({process.returncode})\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        for url in list(pending):
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        pending.remove(url)
            except (OSError, TimeoutError, urllib.error.URLError):
                pass
        if pending:
            time.sleep(0.1)
    if pending:
        raise RuntimeError(f"HTTP endpoints did not become ready: {sorted(pending)}")


def _payload(result) -> dict:
    assert result.content and hasattr(result.content[0], "text")
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_real_mcp_http_transport_auth_write_conflict_and_shutdown(tmp_path: Path):
    gui_port = _unused_port()
    mcp_port = _unused_port()
    while mcp_port == gui_port:
        mcp_port = _unused_port()

    env = {
        **os.environ,
        "HOST": "127.0.0.1",
        "GUI_PORT": str(gui_port),
        "MCP_PORT": str(mcp_port),
        "DB_PATH": str(tmp_path / "data" / "wiki.db"),
        "VAULT_PATH": str(tmp_path / "vault"),
        "EMBEDDING_ENABLED": "false",
        "SESSION_SECRET": "transport-test-session-secret",
        "LOG_LEVEL": "WARNING",
    }
    cli = [sys.executable, "-m", "llm_wiki.cli"]
    admin = subprocess.run(
        [*cli, "create-admin", "--username", "transport-admin", "--password", "secret12"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert admin.returncode == 0, admin.stderr or admin.stdout
    key_result = subprocess.run(
        [*cli, "create-api-key", "--username", "transport-admin", "--name", "transport"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert key_result.returncode == 0, key_result.stderr or key_result.stdout
    api_key = next(line for line in key_result.stdout.splitlines() if line.startswith("lw_"))

    process = subprocess.Popen(
        [*cli, "serve"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout = ""
    stderr = ""
    try:
        _wait_for_health(
            [
                f"http://127.0.0.1:{gui_port}/healthz",
                f"http://127.0.0.1:{mcp_port}/healthz",
            ],
            process,
        )
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=10
        ) as http_client:
            async with streamable_http_client(
                f"http://127.0.0.1:{mcp_port}/mcp", http_client=http_client
            ) as (read_stream, write_stream, _session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    assert "base_version" in (initialized.instructions or "")

                    identity = _payload(await session.call_tool("whoami", {}))
                    assert identity["ok"] is True
                    assert identity["username"] == "transport-admin"

                    created = _payload(
                        await session.call_tool(
                            "create_document",
                            {"path": "transport.md", "content": "# Transport\n\nversion one"},
                        )
                    )
                    assert created["ok"] is True and created["version"] == 1

                    read = _payload(
                        await session.call_tool("read_document", {"path": "transport.md"})
                    )
                    assert read["ok"] is True and read["version"] == 1
                    assert "version one" in read["content"]

                    updated = _payload(
                        await session.call_tool(
                            "update_document",
                            {
                                "path": "transport.md",
                                "base_version": 1,
                                "content": "# Transport\n\nversion two",
                            },
                        )
                    )
                    assert updated["ok"] is True and updated["version"] == 2

                    conflict = _payload(
                        await session.call_tool(
                            "update_document",
                            {
                                "path": "transport.md",
                                "base_version": 1,
                                "content": "# Transport\n\nstale write",
                            },
                        )
                    )
                    assert conflict["ok"] is False
                    assert conflict["error"]["code"] == "conflict"
                    assert conflict["error"]["current_version"] == 2
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
