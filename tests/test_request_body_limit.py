import pytest

from llm_wiki.web.security import RequestBodyLimitMiddleware


async def _call(
    parts: list[bytes], *, max_bytes: int, content_length: str | None = None
) -> tuple[bool, int]:
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
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return called, status


@pytest.mark.asyncio
async def test_content_length_rejected_before_inner_app():
    called, status = await _call([b""], max_bytes=4, content_length="5")
    assert called is False and status == 413


@pytest.mark.asyncio
async def test_streamed_body_rejected_at_actual_limit():
    called, status = await _call([b"abc", b"de"], max_bytes=4)
    assert called is True and status == 413


@pytest.mark.asyncio
async def test_body_at_limit_is_allowed():
    called, status = await _call([b"ab", b"cd"], max_bytes=4)
    assert called is True and status == 204
