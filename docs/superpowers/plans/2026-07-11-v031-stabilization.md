# v0.31 Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the unreleased v0.31 corpus export and existing web/MCP surfaces safe, bounded, metadata-stable, and release-ready.

**Architecture:** Add one reusable pure-ASGI request boundary, repair page script ordering without redesigning rendering, preserve DB metadata only when replacement content carries no metadata signal, stream corpus rows in bounded batches, and derive the runtime version from package metadata. No schema migration is required.

**Tech Stack:** Python 3.12, FastAPI/Starlette ASGI, SQLite/sqlite-vec, Jinja2, vanilla JavaScript, pytest, uv, ruff, mypy.

## Global Constraints

- Python remains `>=3.12,<3.13`; dependency management remains `uv` with `uv.lock`.
- Default request limit is exactly `16 * 1024 * 1024` bytes; configured range is `1 MiB` through `100 MiB`, inclusive.
- Oversized requests are rejected before form/JSON parsing with HTTP `413`.
- MCP's existing 10 MiB attachment remains usable under the 16 MiB base64+JSON request envelope.
- Unmatched HTTP routes use the exact Prometheus label `__unmatched__`.
- Existing CAS conflict behavior and structured error envelopes do not change.
- `llms_full(max_chars=N)` returns `text` with `len(text) <= N` for every non-negative N.
- No schema migration, new color, visual redesign, CDN, or runtime Node dependency.
- `PRODUCT.md` and `DESIGN.md` remain the UI design sources of truth.
- Commit messages and release text contain no tool/vendor attribution; use concise Korean change descriptions.
- Do not modify or stage the user's untracked `AGENTS.md` in the main checkout.

---

### Task 1: Bound HTTP request bodies and cardinality

**Files:**
- Modify: `src/llm_wiki/web/security.py`
- Modify: `src/llm_wiki/web/app.py`
- Modify: `src/llm_wiki/_cli_impl.py`
- Modify: `src/llm_wiki/config.py`
- Modify: `src/llm_wiki/metrics.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_request_id.py`
- Create: `tests/test_request_body_limit.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docker-compose.yml`

**Interfaces:**
- Produces: `RequestBodyLimitMiddleware(app, max_bytes: int)` in `llm_wiki.web.security`.
- Produces: `Settings.request_max_bytes: int`, default `16 * 1024 * 1024`.
- Consumes: Starlette ASGI `scope`, `receive`, and `send`; no route-specific dependency.

- [ ] **Step 1: Write failing middleware and metric tests**

Create `tests/test_request_body_limit.py` with an ASGI harness that records whether the inner app receives the body. Cover:

```python
import pytest

from llm_wiki.web.security import RequestBodyLimitMiddleware


async def _call(parts: list[bytes], *, max_bytes: int, content_length: str | None = None):
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

    headers = [] if content_length is None else [(b"content-length", content_length.encode())]
    scope = {"type": "http", "method": "POST", "path": "/", "raw_path": b"/",
             "query_string": b"", "headers": headers, "http_version": "1.1",
             "scheme": "http", "server": ("test", 80), "client": ("test", 1)}
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
```

Add tests to `tests/test_request_id.py` asserting `_route_label()` returns `__unmatched__` for requests without a matched route and still returns a route template when present. Add `tests/test_config.py` cases for the default and rejection below 1 MiB/above 100 MiB.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_request_body_limit.py tests/test_request_id.py tests/test_config.py -q
```

Expected: collection/import failure for `RequestBodyLimitMiddleware`, raw-path metric assertion failure, and missing setting assertions.

- [ ] **Step 3: Implement the pure-ASGI limit**

In `web/security.py`, export and implement a pure-ASGI middleware. It must pre-reject a valid oversized Content-Length, count actual `http.request` bytes, send one JSON 413 response when the limit is crossed, suppress an inner response after rejection, and pass non-HTTP scopes unchanged. Use this structure:

```python
class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (v for k, v in scope.get("headers", []) if k.lower() == b"content-length"), None)
        try:
            declared = int(content_length) if content_length is not None else None
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        rejected = False

        async def limited_receive():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    await self._reject(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message):
            if not rejected:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not rejected:
                raise

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse({"detail": "Request body too large."}, status_code=413)
        await response(scope, receive, send)
```

Add it to `__all__`. Keep RequestId outermost on web requests by adding the body limiter before `RequestIdMiddleware` in `create_web_app()`. Add the same middleware to the Starlette MCP HTTP app in `_serve()` before Uvicorn starts it.

- [ ] **Step 4: Add settings, metric bound, and deployment docs**

Add `request_max_bytes: int = 16 * 1024 * 1024` and a Pydantic validator enforcing inclusive `[1 * 1024 * 1024, 100 * 1024 * 1024]`. Change `_route_label()` fallback to the literal `__unmatched__`.

Change Compose ports to:

```yaml
ports:
  - "127.0.0.1:8080:8080"
  - "127.0.0.1:8081:8081"
```

Document `REQUEST_MAX_BYTES=16777216` in `.env.example` and README. State that public access should terminate TLS at a reverse proxy and that proxy body limits complement, not replace, the application limit.

- [ ] **Step 5: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_request_body_limit.py tests/test_request_id.py tests/test_config.py tests/test_web.py tests/test_mcp.py -q
uv run ruff check src/llm_wiki/web/security.py src/llm_wiki/web/app.py src/llm_wiki/_cli_impl.py src/llm_wiki/config.py src/llm_wiki/metrics.py tests/test_request_body_limit.py tests/test_request_id.py tests/test_config.py
```

Expected: all selected tests pass and ruff reports `All checks passed!`.

Commit:

```bash
git add src/llm_wiki/web/security.py src/llm_wiki/web/app.py src/llm_wiki/_cli_impl.py src/llm_wiki/config.py src/llm_wiki/metrics.py tests/test_request_body_limit.py tests/test_request_id.py tests/test_config.py .env.example README.md docker-compose.yml
git commit -m "요청 본문 상한과 배포 기본값 강화"
```

---

### Task 2: Restore outlines and heading fragments

**Files:**
- Modify: `src/llm_wiki/web/templates/base.html`
- Modify: `src/llm_wiki/web/templates/view.html`
- Modify: `src/llm_wiki/web/static/outline.js`
- Modify: `tests/test_folders_shell.py`

**Interfaces:**
- Produces: Jinja `page_scripts` block rendered after main/right/status DOM.
- Preserves: existing MutationObserver rebuild and reduced-motion click behavior.

- [ ] **Step 1: Write a failing rendered-order regression test**

Extend `test_view_page_shows_outline_panel_and_statusbar` so it asserts:

```python
html = client.get("/doc/a.md").text
assert html.index('id="outline"') < html.index("/static/outline.js")
js = client.get("/static/outline.js").text
assert "location.hash" in js
assert 'behavior: "auto"' in js
assert "MutationObserver" in js
```

Use the document path already seeded by the fixture; do not add a string-only test that bypasses rendered template order.

- [ ] **Step 2: Run test and verify RED**

Run `uv run pytest tests/test_folders_shell.py::test_view_page_shows_outline_panel_and_statusbar -q`.

Expected: the outline DOM position assertion fails because the script currently appears first.

- [ ] **Step 3: Move page scripts after all page DOM**

In `base.html`, immediately before the shared `shell.js` include, add:

```jinja2
{% block page_scripts %}{% endblock %}
```

Remove the six view-only script tags from the `content` block in `view.html` and define them in a `page_scripts` block after the `statusbar` block. Preserve their current order.

After `build()` assigns heading IDs in `outline.js`, resolve the initial fragment once:

```javascript
var initialHashHandled = false;

function revealInitialHash() {
  if (initialHashHandled || !location.hash) return;
  var id;
  try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { id = location.hash.slice(1); }
  var target = document.getElementById(id);
  if (target) {
    initialHashHandled = true;
    target.scrollIntoView({ behavior: "auto", block: "start" });
  }
}
```

Call `revealInitialHash()` at the end of `build()`. Do not change click-time smooth/reduced-motion behavior.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_folders_shell.py tests/test_section_anchors.py tests/test_web.py -q
node --check src/llm_wiki/web/static/outline.js
```

Expected: all tests and syntax check pass.

Commit:

```bash
git add src/llm_wiki/web/templates/base.html src/llm_wiki/web/templates/view.html src/llm_wiki/web/static/outline.js tests/test_folders_shell.py
git commit -m "문서 목차와 섹션 링크 초기화 복구"
```

---

### Task 3: Preserve explicit document metadata across edits

**Files:**
- Modify: `src/llm_wiki/markdown_utils.py`
- Modify: `src/llm_wiki/services/documents.py`
- Modify: `tests/test_documents.py`
- Modify: `tests/test_targeted_edits.py`

**Interfaces:**
- Produces: `derive_content_title(meta: dict, body: str) -> str | None`.
- Preserves: `DocumentService.update()` signature and current CAS behavior.
- Rule: no explicit/content-derived title or tags means preserve the current DB value.

- [ ] **Step 1: Write failing metadata persistence tests**

Add a test that reproduces the confirmed loss:

```python
def test_explicit_title_and_tags_survive_body_only_update(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    created = docs.create(p, "note.md", "plain body", title="Explicit", tags=["kept"], embed=False)
    updated = docs.update(p, "note.md", created["version"], "plain body changed", embed=False)
    assert updated["title"] == "Explicit"
    assert updated["tags"] == ["kept"]
```

Add targeted-edit coverage for `patch()` and `replace_section()`. Add positive derivation cases proving a new H1 changes the title and a non-empty frontmatter/inline tag set replaces an otherwise-preserved empty signal. Keep `patch_tags(... remove=[...])` as the supported explicit clear path.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_documents.py tests/test_targeted_edits.py -q
```

Expected: explicit metadata becomes path-derived/empty after the first body-only update.

- [ ] **Step 3: Separate content title detection from path fallback**

In `markdown_utils.py`, add:

```python
def derive_content_title(meta: dict, body: str) -> str | None:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    hm = HEADING_RE.search(body)
    return hm.group(2).strip() if hm else None
```

Refactor `derive_title()` to return `derive_content_title(...)` when present and otherwise use the existing path fallback.

- [ ] **Step 4: Resolve metadata inside the CAS writer transaction**

In `DocumentService.update()` compute `meta`, `content_title`, and `derived_tags` before opening the writer, but resolve fallbacks after selecting the current row. Extend the row query to include `title`. Read current tags through `_tags_for_ids(conn, [doc_id])`.

Use these rules:

```python
content_title = derive_content_title(meta, content)
derived_tags = self._merge_tags(meta, content, tags)

# inside writer, after row/doc_id is known
final_title = (title.strip() if title and title.strip()
               else content_title or row["title"])
current_tags = self._tags_for_ids(conn, [doc_id]).get(doc_id, [])
tagset = derived_tags if (tags is not None or derived_tags) else current_tags
```

Leave create behavior unchanged. Do not weaken version comparison or move file/embedding work into the DB transaction.

- [ ] **Step 5: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_documents.py tests/test_targeted_edits.py tests/test_mcp.py tests/test_web.py -q
uv run ruff check src/llm_wiki/markdown_utils.py src/llm_wiki/services/documents.py tests/test_documents.py tests/test_targeted_edits.py
```

Expected: all selected tests pass and ruff is clean.

Commit:

```bash
git add src/llm_wiki/markdown_utils.py src/llm_wiki/services/documents.py tests/test_documents.py tests/test_targeted_edits.py
git commit -m "본문 편집 시 문서 메타데이터 보존"
```

---

### Task 4: Make corpus export bounded and hard-capped

**Files:**
- Modify: `src/llm_wiki/services/documents.py`
- Modify: `tests/test_llms_txt.py`

**Interfaces:**
- Replaces private `_corpus_docs()` list builder with `_iter_corpus_docs(folder=None, batch_size=128)`.
- Preserves public `llms_index()` string and `llms_full()` response keys.
- Guarantees `len(llms_full(...)["text"]) <= max_chars`.

- [ ] **Step 1: Write failing hard-limit and query-shape tests**

Add tests covering:

```python
def test_llms_full_hard_caps_single_large_document(ctx, principals):
    ctx.docs.create(principals["editor"], "big.md", "# Big\n\n" + "가" * 5000, embed=False)
    res = ctx.docs.llms_full(site_title="W", max_chars=1000)
    assert len(res["text"]) <= 1000
    assert res["truncated"] is True


def test_llms_index_escapes_markdown_label(ctx, principals):
    ctx.docs.create(principals["editor"], "odd.md", "body", title="A [B] \\", embed=False)
    text = ctx.docs.llms_index(site_title="W")
    assert r"[A \[B\] \\](/doc/odd.md/raw)" in text
```

Instrument the thread-local SQLite connection with `set_trace_callback`, seed at least three docs, call `llms_index()`, and assert latest bodies are loaded by one query joining `revisions`, not one `SELECT body FROM revisions WHERE doc_id=?` per document. Reset the trace callback in `finally`.

Update the tiny-budget legacy assertion: a budget too small for even the export header may report `included == 0`, but must still be hard-capped and truncated.

- [ ] **Step 2: Run tests and verify RED**

Run `uv run pytest tests/test_llms_txt.py -q`.

Expected: large first document exceeds the budget, special labels are unescaped, and trace shows per-document latest-body selects.

- [ ] **Step 3: Implement a batched corpus iterator**

Replace `_corpus_docs()` with a generator whose query joins the current revision:

```sql
SELECT d.id, d.path, d.title, d.folder, d.updated_at, r.body
FROM documents d
JOIN revisions r ON r.doc_id=d.id AND r.version=d.version
WHERE d.is_deleted=0
ORDER BY d.folder, d.path
```

Apply the optional folder predicate to `d.folder`. Iterate `cursor.fetchmany(batch_size)`, load tags once per batch through `_tags_for_ids`, and yield dictionaries. Add `_corpus_count(folder=None)` for header totals. Never call `_latest_body()` from the iterator.

- [ ] **Step 4: Normalize labels and enforce the final text budget**

Add private helpers:

```python
@staticmethod
def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())

@classmethod
def _md_label(cls, value: object) -> str:
    return cls._one_line(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
```

Use them for site/folder/document labels and descriptions in `llms_index()` and document headings in `llms_full()`.

Build `llms_full()` incrementally. Count the header, each block, separators, and truncation marker. If the next block does not fit, reserve room for the marker, append only the block prefix that fits, increment `included` only when at least one character of that block was emitted, append the marker (or its prefix when the remaining budget is smaller), set `truncated=True`, and stop. Slice once at return as a defensive invariant:

```python
text = "".join(parts)
return {"text": text[:limit], "included": included, "total": total,
        "truncated": truncated or len(text) > limit}
```

Treat `max_chars` as `max(0, int(max_chars))`.

- [ ] **Step 5: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_llms_txt.py tests/test_mcp.py tests/test_web.py -q
uv run ruff check src/llm_wiki/services/documents.py tests/test_llms_txt.py
```

Expected: all selected tests pass and ruff is clean.

Commit:

```bash
git add src/llm_wiki/services/documents.py tests/test_llms_txt.py
git commit -m "코퍼스 export 메모리와 출력 상한 보장"
```

---

### Task 5: Use package metadata as the runtime version source

**Files:**
- Modify: `src/llm_wiki/__init__.py`
- Create: `tests/test_version.py`

**Interfaces:**
- Produces: `llm_wiki.__version__: str` from installed package metadata.

- [ ] **Step 1: Write the failing version consistency test**

Create:

```python
from importlib.metadata import version

import llm_wiki


def test_public_version_matches_package_metadata():
    assert llm_wiki.__version__ == version("llm-wiki")
```

- [ ] **Step 2: Run test and verify RED**

Run `uv run pytest tests/test_version.py -q`.

Expected: `0.1.0 != 0.31.0`.

- [ ] **Step 3: Replace the duplicate literal**

Use:

```python
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("llm-wiki")
except PackageNotFoundError:  # source tree without installed metadata
    __version__ = "0.0.0"
```

Keep the package docstring and do not duplicate `0.31.0` in source.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```bash
uv run pytest tests/test_version.py -q
uv run ruff check src/llm_wiki/__init__.py tests/test_version.py
```

Expected: test and lint pass.

Commit:

```bash
git add src/llm_wiki/__init__.py tests/test_version.py
git commit -m "런타임 버전 표기 단일화"
```

---

### Task 6: Integration verification

**Files:**
- Verify only; production changes require a new failing regression test first.

**Interfaces:**
- Consumes: all five prior task deliverables.

- [ ] **Step 1: Verify repository state and generated assets**

Run:

```bash
git diff --check
git status --short
node --check src/llm_wiki/web/static/outline.js
```

Expected: no whitespace errors, only intended plan/spec or implementation state, JS syntax clean.

- [ ] **Step 2: Run complete quality gates**

Run:

```bash
uv run pytest --cov=llm_wiki --cov-report=term-missing
uv run ruff check .
uv run mypy --check-untyped-defs src/llm_wiki
uv lock --check
```

Expected: all tests pass, coverage does not fall below the 84% baseline, ruff/mypy/lock checks succeed, and no new warnings are introduced beyond the known Starlette TestClient warning.

- [ ] **Step 3: Build and inspect distribution artifacts**

Build outside the repository output path:

```bash
uv build --out-dir /tmp/llm-wiki-v031-dist
uv run python -c 'from importlib.metadata import version; import llm_wiki; assert llm_wiki.__version__ == version("llm-wiki")'
```

Expected: wheel and sdist build, runtime/package versions match. The separate sdist allowlist cleanup remains explicitly deferred to the next work package.

- [ ] **Step 4: Final review handoff**

Generate a whole-branch review package from the branch base and dispatch the final code reviewer. Fix every Critical/Important finding with a failing test and rerun the covering tests before re-review.
