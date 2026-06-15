# llm-wiki

옵시디언(Obsidian)처럼 마크다운 문서를 웹에서 보고 편집하면서, 동시에 LLM이 **HTTP MCP**로 접속해 읽기·검색·쓰기를 할 수 있는 지식베이스입니다.

- 📝 **웹 UI** — 마크다운 뷰어/에디터, `[[위키링크]]`·백링크, 리비전 이력, 링크 그래프 시각화
- 🤖 **HTTP MCP 서버** — LLM 에이전트가 streamable-http로 접속해 문서를 검색/읽기/생성/수정
- 🔎 **하이브리드 검색** — BM25(SQLite FTS5) + 임베딩 벡터(sqlite-vec)를 RRF로 융합
- 🧠 **로컬 임베딩** — HuggingFace `sentence-transformers`(API 키 불필요, 한국어 강함)
- 🔒 **다중 사용자 동시 편집** — 문서별 정수 버전 낙관적 잠금. 앞선 변경이 있으면 **거부**하고 현재 내용을 돌려줘서 재확인 후 재시도. 모든 변경은 작성자·시각과 함께 **전체 본문 스냅샷**으로 기록
- 🕸 **링크 그래프** — 위키링크/마크다운 링크를 파싱해 SQLite에 저장, 백링크·미해석(broken) 링크 추적
- 👤 **역할 기반 권한** — `admin`/`editor`/`viewer`. 웹은 ID/비밀번호 로그인, MCP는 사용자별 API 키(Bearer)

## 요구사항

- [uv](https://docs.astral.sh/uv/) (Python 패키지/런타임 관리)
- Python 3.12 (`.python-version`으로 고정 — uv가 자동 설치/사용. ML 휠 성숙도 때문에 시스템 3.14 대신 3.12를 씁니다)

## 설치

```bash
uv sync          # 의존성 설치 (torch는 CPU 전용 휠로 설치됨)
```

> 첫 `init-db`/`serve` 실행 시 임베딩 모델(`intfloat/multilingual-e5-base`, ~1.1GB)을 HuggingFace에서 자동 다운로드합니다.

## 설정 (`.env`)

`.env.example`를 복사해 `.env`를 만들고 조정합니다.

| 키 | 설명 | 기본값 |
|---|---|---|
| `GUI_PORT` | 웹 UI 포트 | `8080` |
| `MCP_PORT` | MCP 서버 포트 | `8081` |
| `HOST` | 바인드 호스트 | `127.0.0.1` |
| `VAULT_PATH` | 마크다운 `.md` 파일 저장 위치(vault) | `./vault` |
| `DB_PATH` | SQLite DB 경로(메타·리비전·검색인덱스·그래프·사용자) | `./data/llm_wiki.db` |
| `EMBEDDING_MODEL` | 로컬 임베딩 모델 | `intfloat/multilingual-e5-base` |
| `SESSION_SECRET` | 세션 쿠키 서명 키(비우면 자동 생성·DB 저장) | (자동) |

> 임베딩 모델을 바꾸면 벡터 차원이 달라져 기동 시 거부됩니다. `uv run llm-wiki reindex --reembed`로 재임베딩하세요.

## 초기화 & 실행

```bash
uv run llm-wiki init-db                                   # 스키마 생성 + 모델 바인딩
uv run llm-wiki create-admin --username admin             # 첫 관리자(비밀번호 프롬프트)
uv run llm-wiki serve                                     # 웹 + MCP 동시 기동
```

- 웹: `http://127.0.0.1:8080`
- MCP: `http://127.0.0.1:8081/mcp`

## 웹 사용

로그인 후 문서 목록/검색/그래프를 탐색합니다. `editor`/`admin`은 새 문서 작성·편집·삭제가 가능합니다.
편집 저장 시 읽은 시점의 버전(`base_version`)이 함께 전송되며, 그 사이 다른 변경이 있었다면 **충돌 화면**이 떠 현재 서버 내용을 보여줍니다(병합 후 재저장).

- **API 키 발급**: 우측 상단 `API 키` 메뉴에서 발급(한 번만 표시). MCP 접속용입니다.
- **사용자 관리**: `admin`은 `사용자` 메뉴에서 계정 추가/역할 변경/비밀번호 변경/삭제.

## MCP 사용 (LLM 연동)

streamable-http 엔드포인트 `http://<HOST>:<MCP_PORT>/mcp`에 **`Authorization: Bearer <API_KEY>`** 헤더로 접속합니다.

```bash
# API 키 발급(CLI)
uv run llm-wiki create-api-key --username admin --name my-agent
```

제공 MCP 툴:

| 툴 | 권한 | 설명 |
|---|---|---|
| `search_documents(query, mode, top_k, folder?, tags?)` | 읽기 | 하이브리드/BM25/벡터 검색 |
| `read_document(path)` | 읽기 | 본문 + 현재 `version`(=업데이트용 base_version) |
| `list_documents(folder?, tag?, …)` | 읽기 | 문서 목록 |
| `get_links(path)` / `get_backlinks(path)` | 읽기 | 나가는/들어오는 링크 |
| `get_revisions(path)` / `get_revision(path, version)` | 읽기 | 이력/특정 버전 |
| `get_graph(root?, depth, limit, …)` | 읽기 | `{nodes, edges}` 그래프 |
| `create_document(path, content, title?, tags?)` | 쓰기 | 새 문서 |
| `update_document(path, base_version, content, …)` | 쓰기 | **base_version 필수**, 불일치 시 `conflict` 반환 |
| `delete_document(path, base_version?)` | 쓰기 | 소프트 삭제 |

충돌 시 응답 예:

```json
{ "ok": false, "error": {
  "code": "conflict",
  "message": "Update rejected … retry with base_version=7.",
  "current_version": 7,
  "current_content": "…현재 서버 본문…"
}}
```

LLM은 이 `current_content`로 변경을 다시 얹어 `base_version=7`로 재시도하면 됩니다. `viewer` 역할 키로 쓰기 툴 호출 시 `forbidden`을 반환합니다.

## 외부 편집 반영(reindex)

vault의 `.md` 파일을 외부 에디터로 직접 고쳤다면:

```bash
uv run llm-wiki reindex            # mtime/해시 비교로 변경 화해(external-reconcile 리비전 기록)
uv run llm-wiki reindex --reembed  # 임베딩 전체 재생성(모델 교체 시)
```

## 테스트

```bash
uv run pytest
```

## 아키텍처 요약

- **정합성**: DB가 버전/식별/메타데이터의 정본, `.md` 파일은 커밋 후 원자적으로 투영. 크래시 시 `serve` 기동에서 `pending` 파일을 최신 리비전으로 재투영.
- **동시성**: `UPDATE … SET version=version+1 WHERE id=? AND version=?` + rowcount 검사로 race-free CAS. WAL + `BEGIN IMMEDIATE` + busy_timeout.
- **검색 인덱스**: FTS5(BM25) + sqlite-vec(코사인). 임베딩은 쓰기 트랜잭션 밖(커밋 직후)에서 계산.
- **구조**: `db.py`(스키마/연결) · `services/`(auth·documents·users) · `search.py` · `graph.py` · `indexing.py` · `mcp_server.py` · `web/`(FastAPI+Jinja2) · `_cli_impl.py`.

> 그래프 시각화는 Cytoscape.js를 프로젝트에 포함(`src/llm_wiki/web/static/vendor/cytoscape.min.js`)해 자체 서버(`/static/vendor/`)에서 제공하므로 **인터넷 없이 동작**합니다. 버전 업데이트 시 이 파일만 교체하세요.
