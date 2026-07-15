# llm-wiki

옵시디언(Obsidian)처럼 마크다운 문서를 웹에서 보고 편집하면서, 동시에 LLM이 **HTTP MCP**로 접속해 읽기·검색·쓰기를 할 수 있는 지식베이스입니다.

- 📝 **웹 UI** — 옵시디언풍 앱 셸(리본 + 상시 좌측 파일트리 + 본문 + 우측 패널 + 상태바), 마크다운 뷰어/에디터, `[[위키링크]]`·백링크, 리비전 이력, 링크 그래프 시각화
- 🤖 **HTTP MCP 서버** — LLM 에이전트가 streamable-http로 접속해 문서를 검색/읽기/생성/수정
- 📃 **에이전트 친화 포맷(llms.txt)** — `/llms.txt`(문서 색인 사이트맵)·`/llms-full.txt`(전체 본문 한 번에 수집)로 MCP가 아닌 **어떤 LLM 클라이언트도** vault를 발견·통째 수집. 세션 또는 API 키(Bearer)로 접근
- 🔎 **하이브리드 검색** — BM25(SQLite FTS5) + 임베딩 벡터(sqlite-vec)를 RRF로 융합
- 🧠 **로컬 임베딩** — HuggingFace `sentence-transformers`(API 키 불필요, 한국어 강함)
- 🔒 **다중 사용자 동시 편집** — 문서별 정수 버전 낙관적 잠금. 앞선 변경이 있으면 **거부**하고 현재 내용을 돌려줘서 재확인 후 재시도. 모든 변경은 작성자·시각과 함께 **전체 본문 스냅샷**으로 기록
- 🕸 **링크 그래프** — 위키링크/마크다운 링크를 파싱해 SQLite에 저장, 백링크·미해석(broken) 링크 추적
- 👤 **역할 기반 권한** — `admin`/`editor`/`viewer`. 웹은 ID/비밀번호 로그인(+선택 OIDC/SSO), MCP는 사용자별 API 키(Bearer)
- 🛡 **보안 기본기** — 세션 CSRF 토큰 + 동일 출처 검사, 웹 로그인·MCP 인증 레이트리밋, 보안 응답 헤더(CSP·X-Frame-Options 등), 비밀번호 최소 8자, **비밀번호 변경/계정 비활성화 시 세션·API 키 일괄 무효화**
- 🧭 **옵시디언풍 탐색** — 모든 페이지에 상시 좌측 **파일 트리**(폴더 접기/펼치기 + 현재 문서 auto-reveal), **빈 폴더 생성**(구조 먼저, 내용 나중), 트리 우클릭 컨텍스트 메뉴(새 문서/하위 폴더/이름변경·이동/삭제), 좌측 검색·태그 탭, 사이드바 접기·폭조절(localStorage 저장)
- ⌨️ **키보드 워크플로** — **명령 팔레트**(Ctrl/⌘+P)·**퀵 스위처**(Ctrl/⌘+O, 없으면 새 문서 생성)·사이드바 토글(Ctrl/⌘+\\), 에디터 **Ctrl/⌘+S 저장**
- ✍️ **에디터** — [md-editor-rt](https://github.com/imzbf/md-editor-rt)(React + CodeMirror 6) 기반 **좌우 분할 라이브 미리보기**·툴바·소스 문법강조·전체화면. **코드 블록 구문 강조**(highlight.js)와 **위키링크·콜아웃·`==하이라이트==`** 를 미리보기에 더해 실제 보기와 일치, 체크박스·이미지 드래그&드롭/붙여넣기 업로드, 라이트/다크 테마 동기화, 실시간 단어/문자 수(CJK 글자당 집계). 에디터는 단일 번들로 vendoring되어 **오프라인 동작**(외부 CDN 요청 0).
- 📄 **마크다운 확장** — 옵시디언 **콜아웃**(`> [!info]` 등 타입별 색/아이콘), **체크박스 클릭 토글**(읽기 뷰에서 바로), `==하이라이트==`, **코드 블록 구문 강조**(읽기 뷰도 에디터와 동일한 highlight.js·라이트/다크), 문서 **목차(아웃라인)** 우측 패널·헤딩 클릭 스크롤
- 🎨 **편의 기능** — 라이트/다크 **테마 토글**(+OS 자동), 리비전 1‑클릭 롤백·diff 비교, 호버 미리보기, 관련 문서, 태그 색인, 검색 필터(폴더/태그/개수), 모바일 반응형(사이드바 오버레이), 원문(.md) 다운로드, 목록 정렬
- 🔄 **실시간 반영** — WebSocket(`/ws`)으로 문서 변경을 즉시 감지. 뷰어는 본문 라이브 재렌더링, 에디터는 동시 편집 경고. 웹·MCP 어느 쪽 편집이든 반영(**단일 프로세스** 공유 이벤트 버스; 포커스 복귀 시 version 폴링으로 CLI/탭 간 어긋남도 완화)
- 📄 **문서 템플릿** — vault `_templates/*.md`를 새 문서·MCP `create_document(template=)`에 적용
- 🔗 **공개 읽기 링크** — 편집자가 문서별 signed 공유 URL 발급(`/share/<token>`, 로그인 없이 읽기 전용)
- 🔑 **API 키 범위** — 읽기 전용(`read`) / 읽기·쓰기(`readwrite`) 키; 미사용 키는 설정 화면에 표시
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
| `REQUEST_MAX_BYTES` | 웹·MCP 공통 HTTP 요청 본문 상한(바이트, 1–100 MiB) | `16777216` |
| `VAULT_PATH` | 마크다운 `.md` 파일 저장 위치(vault) | `./vault` |
| `DB_PATH` | SQLite DB 경로(메타·리비전·검색인덱스·그래프·사용자) | `./data/llm_wiki.db` |
| `EMBEDDING_MODEL` | 로컬 임베딩 모델 | `intfloat/multilingual-e5-base` |
| `EMBEDDING_REVISION` | (권장) HuggingFace 모델 commit/tag 고정값. 같은 이름의 가중치 변경도 감지 | (없음) |
| `EMBEDDING_ENABLED` | 임베딩 사용 여부. `false`면 모델 로드·벡터 검색/관련문서/RAG 비활성(BM25만) | `true` |
| `SITE_TITLE` | 지식베이스 표시 이름(`/llms.txt`·`/llms-full.txt` 코퍼스 export의 H1) | `llm-wiki` |
| `SESSION_SECRET` | 세션 쿠키 서명 키(비우면 자동 생성·DB 저장) | (자동) |
| `COOKIE_SECURE` | 세션 쿠키 Secure(HTTPS 전용) 플래그. TLS 뒤에서는 `true` | `false` |
| `FORWARDED_ALLOW_IPS` | 신뢰할 리버스 프록시 IP(쉼표 구분 또는 `*`). 프록시 뒤에서 실제 클라이언트 IP를 복원해 스로틀·감사가 정확해짐 | `127.0.0.1` |
| `LOG_LEVEL` | 로그 레벨(DEBUG/INFO/WARNING/ERROR/CRITICAL) | `INFO` |
| `LOG_FILE` | (선택) 크기 로테이션 로그 파일 경로. 비우면 stderr만 | (없음) |
| `SHUTDOWN_GRACE_S` | 종료 시 진행 중 요청을 기다리는 최대 시간(초, 1–300). 오케스트레이터 kill grace 안에서 정상 종료 | `25` |
| `OIDC_ENABLED` | 웹 SSO(OIDC authorization-code + PKCE). `false`면 로컬 아이디/비밀번호만 | `false` |
| `OIDC_ISSUER` | IdP issuer URL (`/.well-known/openid-configuration` 기준). `OIDC_ENABLED=true` 시 필수 | (없음) |
| `OIDC_CLIENT_ID` | OIDC 클라이언트 ID. 활성화 시 필수 | (없음) |
| `OIDC_CLIENT_SECRET` | (선택) confidential client 시크릿. public client는 비움 | (없음) |
| `OIDC_REDIRECT_URI` | 콜백 URL(IdP에 등록). `https://…` 또는 개발용 `http://127.0.0.1`/`localhost`. 활성화 시 필수 | (없음) |
| `OIDC_SCOPES` | 요청 스코프(공백 구분, `openid` 포함) | `openid profile email` |
| `OIDC_DEFAULT_ROLE` | 자동 프로비저닝 사용자 역할(`admin`/`editor`/`viewer`). 기본은 viewer(자동 admin 없음) | `viewer` |
| `OIDC_USERNAME_CLAIM` | 로컬 username으로 쓸 ID 토큰 클레임 | `preferred_username` |
| `OIDC_AUTO_PROVISION` | 매칭 사용자 없을 때 SSO 전용 계정 자동 생성 | `true` |
| `OIDC_ALLOWED_EMAIL_DOMAINS` | 허용 이메일 도메인(쉼표 구분). 비우면 제한 없음 | (없음) |
| `OIDC_REQUIRE_EMAIL_VERIFIED` | 이메일이 있을 때 IdP `email_verified` 요구 | `true` |

> **OIDC/SSO**: 기본 비활성. 켜면 로그인 화면에 **SSO로 로그인**이 추가되며 로컬 비밀번호 로그인은 그대로 동작합니다. MCP는 API 키 전용이며 OIDC를 쓰지 않습니다. 첫 SSO 시 `(issuer, sub)` → 이메일(대소문자 무시) 순으로 기존 계정에 연결하고, 없으면 `OIDC_AUTO_PROVISION`에 따라 `password_hash` 없는 SSO 전용 사용자를 만듭니다.

> 임베딩 모델을 바꾸면 벡터 차원이 달라져 기동 시 거부됩니다. DB를 공유하는 실행 중인 `llm-wiki serve` 프로세스를 모두 중지한 뒤 `uv run llm-wiki reindex --reembed`로 새 모델에 맞춰 벡터 인덱스를 재구성(rebind)하고 전체 재임베딩하고, 완료 후 서버를 다시 시작하세요 — 모델 교체의 지원 경로입니다.

## 초기화 & 실행

```bash
uv run llm-wiki init-db                                   # 스키마 생성 + 모델 바인딩
uv run llm-wiki create-admin --username admin             # 첫 관리자(비밀번호 프롬프트)
uv run llm-wiki serve                                     # 웹 + MCP 동시 기동
```

- 웹: `http://127.0.0.1:8080`
- MCP: `http://127.0.0.1:8081/mcp`

## 웹 사용

로그인 후 화면은 옵시디언풍 **앱 셸**로 구성됩니다 — 맨 왼쪽 **리본**(아이콘 도구막대), 그 옆 **좌측 사이드바**(파일 트리 / 검색 / 태그 탭), 가운데 **본문**, 오른쪽 **패널**(목차·백링크·관련 문서), 맨 아래 **상태바**(단어/문자/백링크 수). 좌·우 사이드바는 접거나 경계선을 끌어 폭을 조절할 수 있고 상태는 브라우저에 저장됩니다. `editor`/`admin`은 새 문서 작성·편집·삭제가 가능합니다.
편집 저장 시 읽은 시점의 버전(`base_version`)이 함께 전송되며, 그 사이 다른 변경이 있었다면 **충돌 화면**이 떠 현재 서버 내용을 보여줍니다(병합 후 재저장).

- **파일 트리 / 폴더**: 모든 페이지 좌측에 폴더·문서 계층 트리가 상시 표시되고, 현재 보는 문서가 자동으로 펼쳐져 강조됩니다(auto-reveal). 트리 헤더의 `＋ 폴더`로 **빈 폴더**를 미리 만들 수 있고(내용 없이도 유지), 문서/폴더를 **우클릭**하면 새 문서·하위 폴더·이름변경(이동)·삭제 메뉴가 뜹니다. 문서 이동 시 그 문서를 가리키던 내부 링크는 보존됩니다.
- **명령 팔레트 / 퀵 스위처**: `Ctrl/⌘+P`로 명령 팔레트(새 문서·그래프·테마 전환·사이드바 토글 등 전 기능 단일 진입), `Ctrl/⌘+O`로 문서 빠른 이동(이름/경로 퍼지검색, 없으면 새 문서 생성). `Ctrl/⌘+\\`은 좌측 사이드바 토글.
- **`/` 슬래시 명령**: 에디터에서 줄 시작 또는 공백 뒤에 `/`를 입력하면 헤딩·목록·할 일·인용·**콜아웃**·표·코드블록·구분선·링크·날짜/시각 삽입 메뉴가 캐럿 위치에 뜹니다(화살표·Enter·Esc).
- **콜아웃 / 체크박스 / 목차**: `> [!info]`·`[!warning]` 등 콜아웃이 타입별 색·아이콘 박스로 렌더됩니다. 읽기 뷰의 `- [ ]` 체크박스는 클릭으로 토글되어 저장됩니다(작성 권한 필요). 우측 패널 `목차` 탭에서 헤딩을 클릭하면 해당 위치로 스크롤합니다.
- **테마**: 리본의 🌓로 라이트/다크/OS자동을 순환 전환합니다(브라우저에 저장).
- **API 키 발급**: 리본의 ⚙️(설정)에서 발급(한 번만 표시). MCP 접속용입니다. **읽기 전용** 키를 선택하면 해당 키로는 문서 쓰기가 거부됩니다. 오래 쓰이지 않은 키는 「미사용」으로 표시됩니다.
- **문서 템플릿**: vault `_templates/` 아래 `.md` 파일이 새 문서 화면의 템플릿 목록에 나타납니다.
- **깨진 링크 정리**: `/broken-links`에서 편집자는 대상 문서를 원클릭 생성할 수 있습니다.
- **태그 관리**: `/tags`에서 편집자는 태그 이름 변경·병합을 할 수 있습니다.
- **그래프 필터**: `/graph`에서 폴더·태그·미해결 링크 표시 여부를 좁힐 수 있습니다.
- **검색 저장**: 검색 워크벤치에서 현재 조건을 브라우저에 이름 붙여 저장·불러오기 합니다.
- **사용자 관리**: `admin`은 리본의 👥(사용자)에서 계정 추가/역할 변경/비밀번호 변경/삭제.
- **깨진 링크**: `깨진 링크` 메뉴(`/broken-links`)에서 존재하지 않는 문서를 가리키는 위키링크/마크다운 링크를 출처 문서와 함께 목록으로 확인·정리.
- **호버 미리보기**: 목록·검색 결과의 문서 제목에 마우스를 올리면 제목과 본문 앞부분 발췌가 팝오버로 표시됩니다.
- **관련 문서**: 문서 뷰어 사이드바에 임베딩 벡터로 의미가 가까운 문서가 유사도(%)와 함께 표시됩니다(명시적 링크가 없어도 발견 가능). 아직 임베딩되지 않은 문서에서는 표시되지 않습니다.
- **실시간 반영**: 문서를 보거나 편집하는 중에 다른 사용자/에이전트(웹 또는 MCP)가 그 문서를 바꾸면 WebSocket(`/ws`)으로 즉시 감지됩니다. 뷰어는 본문을 그 자리에서 다시 렌더링하고(토스트 알림), 에디터는 "다른 곳에서 변경됨, 지금 저장하면 충돌" 경고 배너를 띄웁니다(작성 중 내용은 보존). 삭제/이동도 배너로 안내합니다.

### 검색 워크벤치

검색어에 연산자를 섞어 결과를 좁힐 수 있습니다. 값에 공백이 있으면 `title:"API guide"`처럼 따옴표로 묶습니다.

- `title:`은 제목 포함, `path:`는 경로 포함 또는 `*`·`?` 글롭, `tag:`는 정확한 태그를 찾습니다. `has:`는 `link`(나가는 링크), `backlink`(들어오는 링크), `tag`(태그 보유)를 지원합니다.
- 검색 방식은 **하이브리드**(BM25+벡터), **BM25(어휘)**, **의미(벡터)** 중에서 고릅니다. 예: `/search?q=배포+title:%22API+guide%22+path:notes/*+has:tag&mode=bm25`.
- 요청 태그 필터는 AND 조건이며 `?tag=release&tag=todo`처럼 `tag`를 반복합니다. 페이지를 이동해도 검색어·방식·폴더·반복 태그가 유지됩니다.
- 페이지는 1부터 시작하고 페이지당 결과 수는 1–50 범위입니다(기본 선택지는 10·20·50이며 URL로 지정한 범위 내 값도 유지됩니다). 필터 칩의 `×`를 누르면 같은 값이 여러 번 있어도 선택한 항목 하나만 제거하고 1페이지부터 다시 검색합니다.
- 전체 건수를 확정할 수 있을 때만 `총 N건`을 표시합니다. 그렇지 않으면 정확한 전체 건수를 알 수 없다고 안내하며, 최대 600건 검색 범위에 닿으면 이를 상한으로 명시합니다.

## MCP 사용 (LLM 연동)

streamable-http 엔드포인트 `http://<HOST>:<MCP_PORT>/mcp`에 **`Authorization: Bearer <API_KEY>`** 헤더로 접속합니다.

```bash
# API 키 발급(CLI)
uv run llm-wiki create-api-key --username admin --name my-agent
```

> **키 수명·범위 정책**: API 키에는 시간 기반 만료가 없습니다(장수명 에이전트 사용성 우선). **scope**로 `read`(읽기 전용) 또는 `readwrite`(기본)를 지정할 수 있습니다. 해당 사용자의 **비밀번호가 변경**되거나 **계정이 비활성화**되면 그 사용자의 모든 키가 자동 폐기되고 웹 세션도 함께 무효화됩니다. 비활성 계정에는 새 키를 발급할 수 없으며, 개별 키는 웹 `설정 > API 키`에서 폐기할 수 있습니다.

제공 MCP 툴:

접속 직후 클라이언트에는 서버 **`instructions`**(이 vault의 규약 — 낙관적 잠금·`[[위키링크]]`·토큰 절약 편집 등)가 전달되어 에이전트가 시행착오 없이 올바른 툴을 고를 수 있습니다.

| 툴 | 권한 | 설명 |
|---|---|---|
| `whoami()` | 읽기 | 호출 에이전트의 사용자명·역할·권한(`can_write`/`can_admin`). 쓰기 전 권한 확인 |
| `export_corpus(format, max_chars?)` | 읽기 | vault 전체를 한 덩어리로: `index`=llms.txt 색인, `full`=전체 본문 연결(컨텍스트 일괄 수집) |
| `search_documents(query, mode, top_k, folder?, tags?)` | 읽기 | 하이브리드/BM25/벡터 검색. `count`+`truncated`(top_k 초과 가능성) 반환 |
| `assemble_context(question, max_chars?, max_sources?, mode?, folder?, tags?)` | 읽기 | RAG 1-콜 프리미티브: 하이브리드 랭킹→문서별 최적 구절을 예산 내로 조립해 인용(`[n]`) 태깅된 `context`+`sources` 반환. `search`+`read` 왕복 대체 |
| `get_related_documents(path, limit?)` | 읽기 | 임베딩상 의미가 가까운 문서(링크가 아닌 벡터 기준). `score`=코사인 유사도 |
| `read_document(path, section?, max_chars?)` | 읽기 | 본문 + 현재 `version`. `section`은 헤딩 단위, `max_chars`는 길이 제한 |
| `read_documents(paths, max_chars?)` | 읽기 | 여러 경로를 한 번에 읽기(최대 20). 경로별 성공/실패 항목 |
| `list_templates()` | 읽기 | vault `_templates/` 템플릿 목록 |
| `get_outline(path)` | 읽기 | 헤딩 목록 `{level, text, line}`(섹션 편집 대상 탐색) |
| `list_documents(folder?, tag?, …)` | 읽기 | 문서 목록. `count`/`total`/`has_more`/`sort`로 페이징 |
| `list_recent_changes(limit?, since?, until?)` | 읽기 | 최근 수정 문서(ISO 날짜 범위 필터) |
| `list_activity(…)` / `list_trash(…)` | 편집 | 문서 활동 감사 피드 / 삭제 문서 목록. 삭제자·행위자 메타데이터 보호를 위해 `editor` 이상만 허용 |
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
uv run llm-wiki reindex --reembed  # 임베딩 전체 재생성(모델 교체 시 새 모델로 rebind 후 재임베딩)
```

재색인은 먼저 DB에 남은 `pending` 관리 투영을 최신 리비전으로 복구한 뒤 외부 파일을 읽습니다.
파일이나 DB가 동시에 바뀌면 경로별로 최대 세 번 새 스냅샷을 얻어 재시도하며, 끝까지 수렴하지
않은 경로는 원인과 시도 횟수를 출력하고 종료 코드 `1`을 반환합니다. 단순 누락 파일과 삭제된
문서(tombstone)의 의도적 skip만 있는 경우는 경고를 출력하되 종료 코드 `0`입니다. 이동 중 남은
cleanup 대상 파일이 예상 세대와 다르면 외부 변경을 지우지 않고 보존하며, 안전하게 구분되는 새
세대는 이번 또는 다음 재색인에서 별도 문서로 채택합니다.

## 가져오기(import)

기존 옵시디언 볼트나 마크다운 디렉터리를 한 번에 가져옵니다. 첨부 복사·링크 재작성·충돌 정책·드라이런을 지원합니다.

```bash
uv run llm-wiki import --from ~/Obsidian/MyVault --into notes --dry-run    # 계획만 출력(쓰기 없음)
uv run llm-wiki import --from ~/Obsidian/MyVault --into notes \
  --import-attachments --on-conflict rename                               # 실제 가져오기
```

`--on-conflict`(skip|overwrite|rename) · `--import-attachments`(이미지/첨부 복사 + 링크 재작성) · `--no-recurse` · `--no-embed`(나중에 `reindex --reembed`)를 지원합니다. `--on-conflict overwrite`는 파괴적이라 `--force`가 필요합니다.

CLI 가져오기는 특정 위키 사용자로 로그인한 작업이 아니므로 리비전 작성자는 비워 두고, 감사 로그에는 실제 OS 실행자와 `via=cli`를 기록합니다.

## 백업 / 복원

전체 백업은 **DB + vault + manifest**를 한 파일(`.tar`)로 묶는 `snapshot`을 권장합니다. `restore`는 manifest 검증(스키마 버전 확인)·경로 안전 검사·pending 문서 재투영을 자동으로 처리합니다.

```bash
uv run llm-wiki snapshot --out backups/wiki-$(date +%F).tar   # DB + vault + manifest 한 파일
uv run llm-wiki restore  --in  backups/wiki-2026-06-17.tar    # 복원(대상이 비어있지 않으면 --force)
```

DB만 빠르게 백업하려면 `backup`을 씁니다(WAL 안전: `VACUUM INTO`로 트랜잭션 일관 스냅샷). 단 DB만으로는 불완전하니 vault도 함께 보관하세요.

```bash
uv run llm-wiki backup --out backups/wiki-$(date +%F).db      # DB만(빠른 일관 스냅샷)
```

### 정리(prune)

리비전마다 전체 본문 스냅샷을 보관하고 감사 로그도 계속 쌓이므로 활성 볼트의 DB는 무한히 커집니다. `prune`으로 오래된 리비전·감사 로그를 정리하고 공간을 회수합니다(기본 드라이런, `--force`로 실제 실행).

```bash
uv run llm-wiki prune                                          # 드라이런(무엇이 지워질지 미리보기)
uv run llm-wiki prune --keep 20 --older-than-days 90 --force   # 문서당 최신 20개 유지 + 90일↑ 감사 삭제 + VACUUM
```

> 리비전 정리는 되돌릴 수 없습니다(정리된 버전의 이력·diff·복원이 사라집니다). `--no-vacuum`으로 VACUUM을 건너뛸 수 있습니다.

## 운영 runbook

### 무결성 점검과 감시

```bash
uv run llm-wiki db-check --quick   # 일상 점검: 빠른 SQLite 무결성 + FK/고아 벡터
uv run llm-wiki db-check           # 정기 점검: 전체 integrity_check
```

- `/readyz`의 `pending_files`, `vector_dirty`, `embed_worker`와 Prometheus의
  `llmwiki_pending_files`, `llmwiki_vector_dirty_documents`,
  `llmwiki_embed_worker_consecutive_failures`를 경보에 연결하세요. pending이 0으로
  돌아오지 않거나 worker 실패가 연속되면 쓰기 투영·RAG 품질을 확인합니다.
- DB·vault·모델 볼륨의 여유 공간과 DB 증가율을 함께 감시하세요. 남은 공간 15% 또는
  예상 보존 기간보다 짧아질 때 경보하고, 검증된 snapshot이 있는 상태에서 `prune`합니다.
- `/metrics`는 인증이 없으므로 Prometheus 네트워크에서만 접근하게 하고, HTTP 5xx 비율과
  p95 지연도 함께 경보합니다.

### RPO/RTO와 백업 보관

서비스 중요도에 맞춰 복구 목표를 먼저 정합니다. 예를 들어 일 1회 snapshot이면 장애 직전
최대 24시간 변경을 잃을 수 있으므로 **RPO 24시간**입니다. 실제 **RTO**는 별도 경로에
복원하고 `/readyz`·문서 수·검색을 확인하는 복원 훈련으로 측정하세요.

- snapshot에는 문서 원문, 비밀번호 해시와 세션 서명 정보가 포함됩니다. 로컬 운영 디스크와
  다른 장애 영역에 복사하고, 저장·전송 중 암호화하며, 접근 권한과 보존/삭제 정책을 둡니다.
- 최소 월 1회 빈 임시 DB/vault에 복원해 manifest 검증, `db-check`, 로그인과 대표 검색을
  확인합니다. 백업 파일이 존재한다는 사실만으로 복구 가능하다고 간주하지 않습니다.
- 운영 복원 전 서버를 중지하고 현재 상태도 별도 보존합니다. `restore`의 `--force`는 대상
  내용을 교체하므로 복원 파일과 경로를 다시 확인한 뒤 사용합니다.

### 안전한 업그레이드와 롤백

1. 현재 버전으로 전체 snapshot을 만들고 외부 보관소 복사까지 확인합니다.
2. 서버를 정상 종료하고 새 코드를 받은 뒤 `uv sync --locked`로 잠금파일 그대로 설치합니다.
3. 기존 DB가 모델 revision 추적 도입 전의 임베딩 벡터를 갖고 있으면 `init-db`/`serve`가
   검증할 수 없는 벡터 사용을 거부합니다. 안내에 따라 모든 서버가 멈춘 상태에서
   `uv run llm-wiki reindex --reembed`를 한 번 실행해 현재 모델·revision으로 다시 바인딩합니다.
4. `uv run llm-wiki db-check --quick`을 실행하고 서버를 시작합니다. 스키마 마이그레이션은
   시작 시 적용됩니다.
5. 웹·MCP `/healthz`, 웹 `/readyz`, 로그인, 대표 문서 읽기/검색을 확인합니다.

새 스키마가 적용된 DB에 예전 바이너리를 그대로 연결하는 방식은 안전한 롤백이 아닙니다.
롤백해야 하면 서버를 중지하고 **업그레이드 전 snapshot**을 예전 버전의 빈 대상에 복원하세요.
`SHUTDOWN_GRACE_S`는 오케스트레이터의 종료 유예보다 짧게 두어 진행 중 쓰기가 SIGKILL로
끊기지 않게 합니다.

## 도커로 실행

```bash
mkdir -p data vault                                            # uid 1000이 쓸 호스트 경로
docker compose run --rm llm-wiki create-admin --username admin   # 최초 1회 관리자 생성
docker compose up -d                                             # 웹(8080) + MCP(8081) 기동
```

- torch는 pyproject의 `pytorch-cpu` 인덱스를 따라 **CPU 휠**로 설치되어 이미지가 가볍습니다.
- 임베딩 모델 캐시는 `hf-models` 볼륨에 보존되어 재시작 시 재다운로드하지 않습니다.
- `./data`(DB)·`./vault`(문서)는 호스트에 바인드 마운트됩니다. TLS 뒤라면 `COOKIE_SECURE=true`를 설정하세요.
- 컨테이너는 기본 uid/gid 1000의 non-root 사용자, 읽기 전용 root filesystem, capability 없음으로 실행됩니다. 호스트 uid/gid가 다르면 `LLM_WIKI_UID`·`LLM_WIKI_GID`를 지정해 다시 빌드하세요.
- Compose 포트는 기본적으로 호스트의 루프백에만 바인딩됩니다. 공개 접근은 TLS를 종료하는 리버스 프록시를 통하고, 프록시의 요청 본문 상한도 설정하세요. 프록시 상한은 애플리케이션의 `REQUEST_MAX_BYTES`를 보완할 뿐 대체하지 않습니다.
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

> 프런트엔드 서드파티 라이브러리는 모두 프로젝트에 포함(`src/llm_wiki/web/static/vendor/`)해 자체 서버(`/static/vendor/`)에서 제공하므로 **인터넷 없이 동작**합니다 — 그래프 시각화 Cytoscape.js(`cytoscape.min.js`), 마크다운 에디터 번들(`md-editor.bundle.js`·`md-editor.bundle.css`), 읽기 뷰 코드 강조(`hljs.bundle.js`·`hljs-theme.css`). 이들은 `frontend/`(React + md-editor-rt + highlight.js)를 esbuild로 빌드한 결과물이며, 빌드 산출물을 커밋하므로 **런타임에는 Node가 필요 없습니다**. 프런트엔드를 수정할 때만 빌드하세요:
>
> ```bash
> cd frontend && npm install && npm run build   # -> web/static/vendor/md-editor.bundle.{js,css}
> ```

## 라이선스와 보안

코드는 [MIT License](LICENSE)로 배포됩니다. 취약점은 공개 이슈 대신
[보안 정책](SECURITY.md)의 비공개 신고 경로를 이용해 주세요.
