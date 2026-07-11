import json

import pytest
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.testclient import TestClient

from llm_wiki.config import Settings
from llm_wiki.services.documents import ATTACH_MAX_BYTES
from llm_wiki.web import create_web_app
from llm_wiki.web.security import RequestBodyLimitMiddleware


async def _call(
    parts: list[bytes], *, max_bytes: int, content_length: str | None = None
) -> tuple[bool, list[int]]:
    called = False
    sent: list[dict] = []
    messages = [
        {"type": "http.request", "body": part, "more_body": i < len(parts) - 1}
        for i, part in enumerate(parts)
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def inner(scope, receive, send):
        nonlocal called
        called = True
        while True:
            msg = await receive()
            if msg["type"] != "http.request" or not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    headers = (
        []
        if content_length is None
        else [(b"content-length", content_length.encode())]
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
    }
    await RequestBodyLimitMiddleware(inner, max_bytes=max_bytes)(scope, receive, send)
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    return called, statuses


@pytest.mark.asyncio
async def test_content_length_rejected_before_inner_app():
    called, statuses = await _call([b""], max_bytes=4, content_length="5")
    assert called is False and statuses == [413]


@pytest.mark.asyncio
async def test_streamed_body_rejected_at_actual_limit():
    called, statuses = await _call([b"abc", b"de"], max_bytes=4)
    assert called is True and statuses == [413]


@pytest.mark.asyncio
async def test_body_at_limit_is_allowed():
    called, statuses = await _call([b"ab", b"cd"], max_bytes=4)
    assert called is True and statuses == [204]


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_unchanged():
    scope = {"type": "websocket", "path": "/events"}
    incoming = {"type": "websocket.connect"}
    outgoing = {"type": "websocket.close", "code": 1000}
    seen = {}
    sent = []

    async def receive():
        return incoming

    async def send(message):
        sent.append(message)

    async def inner(inner_scope, inner_receive, inner_send):
        seen["scope"] = inner_scope
        seen["message"] = await inner_receive()
        await inner_send(outgoing)

    await RequestBodyLimitMiddleware(inner, max_bytes=1)(scope, receive, send)

    assert seen == {"scope": scope, "message": incoming}
    assert sent == [outgoing]


@pytest.mark.asyncio
async def test_stream_overflow_after_response_start_does_not_send_second_start():
    sent = []
    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"de", "more_body": False},
    ]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    async def inner(_scope, receive, send):
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await receive()
        assert (await receive())["type"] == "http.disconnect"
        await send({"type": "http.response.body", "body": b"accepted"})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
    }
    await RequestBodyLimitMiddleware(inner, max_bytes=4)(scope, receive, send)

    starts = [message["status"] for message in sent if message["type"] == "http.response.start"]
    assert starts == [202]
    assert sent[-1] == {"type": "http.response.body", "body": b"accepted"}


def test_web_app_rejects_oversized_multipart_before_parser_or_handler(
    ctx, monkeypatch
):
    one_mib = 1024 * 1024
    ctx.settings = ctx.settings.model_copy(update={"request_max_bytes": one_mib})
    entered = {"form_parser": 0, "upload_handler": 0}

    async def track_form_parser(_request, *args, **kwargs):
        entered["form_parser"] += 1
        return FormData()

    def track_upload_handler(*args, **kwargs):
        entered["upload_handler"] += 1
        return {}

    monkeypatch.setattr(Request, "_get_form", track_form_parser)
    monkeypatch.setattr(ctx.docs, "save_attachment", track_upload_handler)

    with TestClient(create_web_app(ctx)) as client:
        response = client.post(
            "/api/upload",
            files={"file": ("large.png", bytes(one_mib + 1), "image/png")},
            headers={"X-Request-ID": "web-body-limit-test"},
        )

    assert response.status_code == 413
    assert response.headers["X-Request-ID"] == "web-body-limit-test"
    assert entered == {"form_parser": 0, "upload_handler": 0}


def test_default_request_limit_exceeds_attachment_limit():
    assert ATTACH_MAX_BYTES == 10 * 1024 * 1024
    assert Settings().request_max_bytes == 16 * 1024 * 1024
    assert ATTACH_MAX_BYTES < Settings().request_max_bytes


def test_cli_mcp_app_rejects_oversized_json_before_transport(ctx, monkeypatch):
    from llm_wiki._cli_impl import _create_mcp_http_app

    one_mib = 1024 * 1024
    ctx.settings = ctx.settings.model_copy(update={"request_max_bytes": one_mib})
    mcp_app = _create_mcp_http_app(ctx)
    mcp_route = next(route for route in mcp_app.routes if route.path == "/mcp")
    session_manager = mcp_route.endpoint.session_manager
    original_handle_request = session_manager.handle_request
    entered = {"transport": 0}

    async def track_transport(scope, receive, send):
        entered["transport"] += 1
        await original_handle_request(scope, receive, send)

    monkeypatch.setattr(session_manager, "handle_request", track_transport)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "body-limit-test", "version": "1"},
        },
        "padding": "x" * one_mib,
    }

    with TestClient(mcp_app, base_url="http://localhost:8081") as client:
        response = client.post(
            "/mcp",
            content=json.dumps(request),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 413
    assert entered == {"transport": 0}
