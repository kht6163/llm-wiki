# llm-wiki

옵시디언(Obsidian)처럼 마크다운 문서를 웹에서 보고 편집하면서, 동시에 LLM이 **HTTP MCP**로 접속해 읽기·검색·쓰기를 할 수 있는 지식베이스입니다.

- 📝 **웹 UI** — 마크다운 뷰어/에디터, `[[위키링크]]`·백링크, 리비전 이력, 링크 그래프 시각화
- 🤖 **HTTP MCP 서버** — LLM 에이전트가 streamable-http로 접속해 문서를 검색/읽기/생성/수정
- 🔎 **하이브리드 검색** — BM25(SQLite FTS5) + 임베딩 벡터(sqlite-vec)를 RRF로 융합
- 🧠 **로컬 임베딩** — HuggingFace `sentence-transformers`(API 키 불필요, 한국어 강함)
- 🔒 **다중 사용자 동시 편집** — 문서별 정수 버전 낙관적 잠금. 앞선 변경이 있으면 **거부**하고 현재 내용을 돌려줘서 재확인 후 재시도. 모든 변경은 작성자·시각과 함께 **전체 본문 스냅샷**으로 기록
- 🕸 **링크 그래프** — 위키링크/마크다운 링크를 파싱해 SQLite에 저장, 백링크·미해석(broken) 링크 추적
- 👤 **역할 기반 권한** — `admin`/`editor`/`viewer`. 웹은 ID/비밀번호 로그인, MCP는 사용자별 API 키(Bearer)
- 🛡 **보안 기본기** — 세션 CSRF 토큰 + 동일 출처 검사, 웹 로그인·MCP 인증 레이트리밋, 보안 응답 헤더(CSP·X-Frame-Options 등), 비밀번호 최소 8자, **비밀번호 변경/계정 비활성화 시 세션·API 키 일괄 무효화**
- 🎨 **편의 기능** — 다크모드(OS 설정 자동), 리비전 1‑클릭 롤백·diff 비교, 에디터 라이브 프리뷰·`[[위키링크]]` 자동완성·서식 단축키(Ctrl/⌘+B/I/K·Tab)·이미지 드래그&드롭/붙여넣기 업로드, 폴더 사이드바·선택, 태그 색인, 검색 필터(폴더/태그/개수), 모바일 반응형, 원문(.md) 다운로드, 목록 정렬
- 🔄 **실시간 반영** — WebSocket(`/ws`)으로 문서 변경을 즉시 감지. 뷰어는 본문 라이브 재렌더링, 에디터는 동시 편집 경고. 웹·MCP 어느 쪽 편집이든 반영(단일 프로세스 공유 이벤트 버스)
- 🩺 **운영** — 스키마 마이그레이션(버전 가드), 구조화 로깅(+로테이션 파일) + 감사 로그(`audit_log`), `/healthz`·`/readyz` 헬스체크, Prometheus `/metrics`, 설정 검증(기동 전), 기동 시 모델 워밍

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
| `COOKIE_SECURE` | 세션 쿠키 Secure(HTTPS 전용) 플래그. TLS 뒤에서는 `true` | `false` |
| `LOG_LEVEL` | 로그 레벨(DEBUG/INFO/WARNING/ERROR/CRITICAL) | `INFO` |
| `LOG_FILE` | (선택) 크기 로테이션 로그 파일 경로. 비우면 stderr만 | (없음) |
| `SHUTDOWN_GRACE_S` | 종료 시 진행 중 요청을 기다리는 최대 시간(초, 1–300). 오케스트레이터 kill grace 안에서 정상 종료 | `25` |

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
- **깨진 링크**: `깨진 링크` 메뉴(`/broken-links`)에서 존재하지 않는 문서를 가리키는 위키링크/마크다운 링크를 출처 문서와 함께 목록으로 확인·정리.
- **호버 미리보기**: 목록·검색 결과의 문서 제목에 마우스를 올리면 제목과 본문 앞부분 발췌가 팝오버로 표시됩니다.
- **관련 문서**: 문서 뷰어 사이드바에 임베딩 벡터로 의미가 가까운 문서가 유사도(%)와 함께 표시됩니다(명시적 링크가 없어도 발견 가능). 아직 임베딩되지 않은 문서에서는 표시되지 않습니다.
- **실시간 반영**: 문서를 보거나 편집하는 중에 다른 사용자/에이전트(웹 또는 MCP)가 그 문서를 바꾸면 WebSocket(`/ws`)으로 즉시 감지됩니다. 뷰어는 본문을 그 자리에서 다시 렌더링하고(토스트 알림), 에디터는 "다른 곳에서 변경됨, 지금 저장하면 충돌" 경고 배너를 띄웁니다(작성 중 내용은 보존). 삭제/이동도 배너로 안내합니다.

## MCP 사용 (LLM 연동)

streamable-http 엔드포인트 `http://<HOST>:<MCP_PORT>/mcp`에 **`Authorization: Bearer <API_KEY>`** 헤더로 접속합니다.

```bash
# API 키 발급(CLI)
uv run llm-wiki create-api-key --username admin --name my-agent
```

> **키 수명 정책**: API 키에는 시간 기반 만료가 없습니다(장수명 에이전트 사용성 우선). 대신 해당 사용자의 **비밀번호가 변경**되거나 **계정이 비활성화**되면 그 사용자의 모든 키가 자동 폐기되고 웹 세션도 함께 무효화됩니다. 개별 키는 웹 `설정 > API 키`에서 폐기할 수 있습니다.

제공 MCP 툴:

| 툴 | 권한 | 설명 |
|---|---|---|
| `search_documents(query, mode, top_k, folder?, tags?)` | 읽기 | 하이브리드/BM25/벡터 검색. `count`+`truncated`(top_k 초과 가능성) 반환 |
| `assemble_context(question, max_chars?, max_sources?, mode?, folder?, tags?)` | 읽기 | RAG 1-콜 프리미티브: 하이브리드 랭킹→문서별 최적 구절을 예산 내로 조립해 인용(`[n]`) 태깅된 `context`+`sources` 반환. `search`+`read` 왕복 대체 |
| `get_related_documents(path, limit?)` | 읽기 | 임베딩상 의미가 가까운 문서(링크가 아닌 벡터 기준). `score`=코사인 유사도 |
| `read_document(path, section?, max_chars?)` | 읽기 | 본문 + 현재 `version`. `section`은 헤딩 단위, `max_chars`는 길이 제한 |
| `get_outline(path)` | 읽기 | 헤딩 목록 `{level, text, line}`(섹션 편집 대상 탐색) |
| `list_documents(folder?, tag?, …)` | 읽기 | 문서 목록. `count`/`total`/`has_more`/`sort`로 페이징 |
| `list_recent_changes(limit?, since?, until?)` | 읽기 | 최근 수정 문서(ISO 날짜 범위 필터) |
| `list_broken_links(limit?)` | 읽기 | 저장소 전체 미해석(깨진) 링크 목록 |
| `get_tags()` | 읽기 | 태그 목록 + 사용 횟수(필터용 어휘 탐색) |
| `get_links(path)` / `get_backlinks(path)` | 읽기 | 나가는/들어오는 링크 |
| `get_revisions(path)` / `get_revision(path, version)` | 읽기 | 이력/특정 버전 |
| `get_graph(root?, depth, limit, …)` | 읽기 | `{nodes, edges}` 그래프 |
| `create_document(path, content, title?, tags?)` | 쓰기 | 새 문서 |
| `update_document(path, base_version, content, …)` | 쓰기 | **base_version 필수**, 불일치 시 `conflict` 반환 |
| `patch_document(path, find, replace, base_version?, count?)` | 쓰기 | 고유 문자열 치환(토큰 절약). 모호하면 `validation` |
| `replace_section / append_section(path, heading, text, base_version?)` | 쓰기 | 헤딩 섹션 단위 편집(전체 본문 불필요). `base_version`으로 충돌 방지 |
| `patch_tags(path, add?, remove?)` | 쓰기 | 본문 수정 없이 frontmatter 태그 추가/제거 |
| `move_document(path, new_path)` | 쓰기 | 이름 변경/이동(이력 보존·링크 재해석) |
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

## 백업 / 복원

WAL이 켜진 상태에서 `.db` 파일을 단순 복사하면 손상된(torn) 스냅샷이 나올 수 있습니다.
`backup` 명령은 `VACUUM INTO`로 트랜잭션 일관성이 보장된 온라인 스냅샷을 만듭니다.

```bash
uv run llm-wiki backup --out backups/wiki-$(date +%F).db   # DB 일관 스냅샷
```

완전한 백업은 **DB 스냅샷 + vault 디렉터리**를 함께 보관해야 합니다(본문은 vault의 `.md`에도 투영됨).
복원은 스냅샷을 `DB_PATH`로, vault 백업을 `VAULT_PATH`로 되돌린 뒤 필요 시 `reindex`를 실행합니다.

## 도커로 실행

```bash
docker compose run --rm llm-wiki create-admin --username admin   # 최초 1회 관리자 생성
docker compose up -d                                             # 웹(8080) + MCP(8081) 기동
```

- torch는 pyproject의 `pytorch-cpu` 인덱스를 따라 **CPU 휠**로 설치되어 이미지가 가볍습니다.
- 임베딩 모델 캐시는 `hf-models` 볼륨에 보존되어 재시작 시 재다운로드하지 않습니다.
- `./data`(DB)·`./vault`(문서)는 호스트에 바인드 마운트됩니다. TLS 뒤라면 `COOKIE_SECURE=true`를 설정하세요.
- 헬스체크: 웹 `/healthz`(liveness)·`/readyz`(DB + 모델 로드 확인), MCP `:8081/healthz`. 이미지/compose에 `HEALTHCHECK`·메모리 제한·`no-new-privileges`가 적용됩니다.
- 메트릭: Prometheus 노출 엔드포인트 `/metrics`(웹·MCP 양쪽 포트, 공유 레지스트리). 무인증이므로 스크레이프 대상 외에는 네트워크 레벨에서 차단하세요. `LOG_FILE`로 로테이션 로그 파일을 남길 수 있습니다.

## 테스트 & 품질

```bash
uv run pytest                 # 단위 + 라우트/MCP/보안 테스트
uv run ruff check .           # 린트
uv run mypy src/llm_wiki      # 타입 체크
```

`.github/workflows/ci.yml`이 push/PR마다 위 셋을 실행합니다(HuggingFace 모델 캐시 포함).

## 아키텍처 요약

- **정합성**: DB가 버전/식별/메타데이터의 정본, `.md` 파일은 커밋 후 원자적으로 투영. 크래시 시 `serve` 기동에서 `pending` 파일을 최신 리비전으로 재투영.
- **동시성**: `UPDATE … SET version=version+1 WHERE id=? AND version=?` + rowcount 검사로 race-free CAS. WAL + `BEGIN IMMEDIATE` + busy_timeout.
- **검색 인덱스**: FTS5(BM25) + sqlite-vec(코사인). 임베딩은 쓰기 트랜잭션 밖(커밋 직후)에서 계산. 폴더/태그 필터는 BM25 후보 질의에 푸시다운되어 대형 vault에서도 리콜이 유지됩니다.
- **CJK 검색 참고**: FTS5는 `unicode61` 토크나이저라 공백으로 띄어쓴 한국어 산문은 정상 색인되지만, 공백 없는 합성어 내부 substring(또는 띄어쓰기 없는 CJK 언어)은 BM25 단독으로 못 찾을 수 있습니다. 이 경우 기본 **하이브리드** 모드의 의미(벡터) 검색이 보완합니다.
- **구조**: `db.py`(스키마/연결) · `services/`(auth·documents·users·audit) · `search.py` · `graph.py` · `indexing.py` · `mcp_server.py` · `web/`(FastAPI+Jinja2, `/ws` 실시간) · `events.py`(인프로세스 변경 이벤트 버스) · `metrics.py`·`ratelimit.py`·`logconf.py`·`config.py` · `_cli_impl.py`.

> 그래프 시각화는 Cytoscape.js를 프로젝트에 포함(`src/llm_wiki/web/static/vendor/cytoscape.min.js`)해 자체 서버(`/static/vendor/`)에서 제공하므로 **인터넷 없이 동작**합니다. 버전 업데이트 시 이 파일만 교체하세요.
