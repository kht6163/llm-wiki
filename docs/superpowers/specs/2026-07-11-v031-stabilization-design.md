# v0.31 안정화 설계

## 목표

`v0.31.0` 정식 발행 전에 외부 요청의 자원 상한, 문서 목차, 제목·태그 보존,
`llms.txt` 코퍼스 export의 출력 계약, 런타임 버전 표기를 안정화한다. 새 기능을
추가하지 않고 현재 공개된 계약을 지키는 데 집중한다.

## 범위

이번 묶음에 포함한다.

1. 웹·MCP 요청 본문을 파싱 전에 제한하고, 404 메트릭 label을 유한하게 만든다.
2. 문서 목차와 검색 결과의 heading fragment를 복구한다.
3. 명시적으로 저장된 제목·태그가 후속 본문 편집에서 사라지지 않게 한다.
4. 코퍼스 export를 bounded-memory로 만들고 `max_chars`를 실제 hard limit로 지킨다.
5. `llm_wiki.__version__`과 설치 패키지 버전을 단일 소스로 통합한다.
6. Compose 기본 포트는 loopback에만 공개하고, 외부 공개 시 reverse proxy/TLS를
   사용하도록 문서화한다.

다음 묶음으로 미룬다.

- reindex 동시성, move/purge crash recovery, 모델 rebind 복구 원장
- Playwright/axe CI, frontend lockfile, sdist allowlist, coverage/type ratchet
- 검색 golden evaluation과 MCP tool profile
- GitHub release 생성, 태그·푸시 등 외부 상태 변경

## 접근 방식 비교

### A. 애플리케이션 경계에서 수술적으로 보강 — 선택

공통 순수-ASGI 요청 제한기를 웹과 MCP 양쪽에 적용하고, 기존 서비스 API는 작은
계약 변경만 한다. 목차는 템플릿의 script 실행 순서를 고치고, export는 iterator와
출력 budget 계산을 도입한다.

- 장점: reverse proxy 유무와 무관하게 보호되고, 기존 아키텍처·스키마를 유지한다.
- 단점: 요청 제한과 export budget을 애플리케이션 코드가 직접 관리한다.

### B. reverse proxy 설정에만 의존

Nginx/Caddy의 body limit과 TLS 설정만 문서화한다.

- 장점: 코드 변경이 작다.
- 단점: 직접 실행과 잘못된 배포를 보호하지 못하고, 테스트 가능한 제품 계약이 아니다.

### C. 서비스 계층을 먼저 대규모 분리

웹/MCP/문서 서비스를 재구성하면서 각 문제를 함께 해결한다.

- 장점: 장기 구조가 깔끔해질 수 있다.
- 단점: 릴리즈 직전 변경 범위와 회귀 위험이 지나치게 크며, 확인된 결함 해결을 늦춘다.

이번 안정화에는 A를 사용한다. 구조 개편은 회귀 테스트와 운영 계약이 강화된 이후에
별도 계획으로 진행한다.

## 설계

### 1. 요청 경계와 배포 기본값

`RequestBodyLimitMiddleware`를 순수 ASGI middleware로 구현한다. 기본 상한은
`16 MiB`이며 `REQUEST_MAX_BYTES` 설정으로 조정한다. 설정 허용 범위는
`1 MiB` 이상 `100 MiB` 이하로 한다.

middleware는 HTTP 요청에 대해 두 겹으로 검사한다.

1. 유효한 `Content-Length`가 상한보다 크면 body를 읽지 않고 즉시 `413`을 반환한다.
2. `receive()`를 감싸 chunked/잘못된 Content-Length 요청의 실제 누적 바이트가
   상한을 넘는 순간 `413`으로 중단한다.

WebSocket과 lifespan scope에는 관여하지 않는다. 웹에서는 Session/CSRF/multipart
파싱보다 바깥에서 실행하고, MCP에서는 transport의 `request.body()`보다 바깥에서
실행한다. reverse proxy limit은 방어 심층화이며 애플리케이션 limit을 대체하지 않는다.

MCP의 10 MiB attachment는 base64와 JSON overhead를 포함해 기본 16 MiB 안에
들어간다. 제한을 넘는 문서·배치 편집은 작은 호출로 나누도록 README에 안내한다.

Prometheus에서 route가 매칭되지 않은 요청은 실제 URL 대신 `__unmatched__` label을
쓴다. Compose의 기본 publish 주소는 `127.0.0.1`로 제한한다.

### 2. 목차와 fragment

`base.html`에 모든 본문·우측 패널·상태바 뒤에서 실행되는 `page_scripts` block을
추가한다. `view.html`의 view 전용 script를 이 block으로 옮겨 `#outline`과
`#rp-related`가 생성된 뒤 초기화되도록 한다.

`outline.js`는 heading id를 만든 뒤 현재 `location.hash`가 가리키는 heading이 있으면
초기 진입에서 `scrollIntoView({behavior: "auto"})`로 맞춘다. 사용자가 목차를 클릭할
때의 reduced-motion/smooth-scroll 정책은 유지한다. 실시간 본문 교체 후 기존
`MutationObserver`가 목차를 다시 만든다.

### 3. 제목·태그 보존 계약

갱신 시 metadata 우선순위를 다음으로 고정한다.

1. 호출자가 `title` 또는 `tags`를 명시하면 그 값을 사용한다.
2. 새 본문에 frontmatter title, H1, frontmatter tags 또는 inline hashtag가 있으면
   해당 값을 다시 파생한다.
3. 새 본문에 해당 metadata 신호가 하나도 없으면 현재 DB의 제목·태그를 보존한다.

태그 전체 제거는 이미 존재하는 `patch_tags` 전용 경로를 사용한다. 따라서 본문에
태그가 없다는 이유만으로 저장된 태그를 암묵적으로 모두 지우지 않는다. `update()`가
현재 row와 tag set을 같은 writer transaction에서 읽고 최종 metadata를 결정해 CAS
쓰기와 일관성을 유지한다.

### 4. 코퍼스 export

문서와 현재 revision은 `documents.version = revisions.version` join으로 한 번에 읽는다.
cursor `fetchmany()`와 batch tag 조회를 결합한 iterator가 최대 한 batch의 본문만
메모리에 둔다. `llms_index()`와 `llms_full()`은 같은 iterator를 공유한다.

`llms_full(max_chars=N)`의 최종 `text`는 항상 `len(text) <= N`이어야 한다. header,
문서 header, 본문, separator, truncation marker를 모두 budget에 포함한다. 첫 문서가
남은 budget보다 크면 그 문서 block을 가능한 길이까지만 포함하고 `truncated=true`로
끝낸다. `included`는 일부라도 표현된 문서 수를 센다. 기존 응답 키는 유지한다.

사이트 제목·폴더명·문서 제목·설명은 한 줄로 정규화하고 Markdown link label의
역슬래시와 대괄호를 escape한다. URL path는 기존 percent-encoding을 유지한다.

### 5. 버전 단일 소스

`llm_wiki.__version__`은 `importlib.metadata.version("llm-wiki")`에서 읽는다. 배포
metadata를 찾을 수 없는 소스-only 환경에서는 `"0.0.0"`을 반환한다. 별도의 버전
문자열을 소스에 중복하지 않는다.

## 오류 처리

- oversized request는 애플리케이션 handler에 진입하지 않고 HTTP `413`을 반환한다.
- 잘못된 `Content-Length`는 신뢰하지 않고 실제 바이트 누적 검사로 보호한다.
- metadata CAS 충돌은 기존 `ConflictError` 계약을 그대로 유지한다.
- export는 budget 초과를 예외로 만들지 않고 `truncated=true`로 보고한다.

## 테스트 전략

모든 production 변경은 RED → GREEN 순으로 구현한다.

- 요청 제한: Content-Length 초과, chunked 초과, 정확히 상한, non-HTTP scope,
  웹 multipart pre-parse 차단, MCP ASGI transport 차단
- 메트릭: 서로 다른 임의 404가 모두 `__unmatched__` 하나로 집약
- 목차: 렌더된 HTML에서 target DOM이 script보다 먼저 등장, 초기 hash 처리,
  MutationObserver 유지
- metadata: 명시 title/tags 생성 후 full update·patch·section edit에서 보존,
  새 H1/frontmatter가 있을 때 재파생, patch_tags로 전체 제거
- export: 첫 문서가 budget보다 큰 경우, header/marker 포함 hard cap, 다문서 경계,
  특수문자 label, batch iterator가 N+1 latest-body 조회를 만들지 않음
- 버전: `llm_wiki.__version__ == importlib.metadata.version("llm-wiki")`
- 회귀: 관련 테스트 파일, 전체 pytest, ruff, mypy

## 완료 조건

- 위 테스트가 모두 통과한다.
- 전체 기존 테스트에 회귀가 없다.
- `ruff check .`와 `mypy src/llm_wiki`가 통과한다.
- README와 `.env.example`가 새 요청 상한 및 loopback/TLS 배포 기본값을 설명한다.
- 사용자의 기존 미추적 `AGENTS.md`를 건드리지 않고, 구현 변경은 격리 worktree에서만 한다.
